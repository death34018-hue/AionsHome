"""Tool-neutral MCP visitor operations backed by the existing lounge services."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4

from visitor_lounge.container import Container
from visitor_lounge.models import GenerationJobRecord, Message, QuotaState, VisitorRecord
from visitor_lounge.prompts import PromptBudgetExceeded, VisitorInputTooLong
from visitor_lounge.quota import (
    QuotaExhausted,
    RequestConflict,
    VisitorHasPendingJob,
)
from visitor_lounge.reception_settings import ReceptionSettingsRepository
from visitor_lounge.repository import (
    BackgroundRepository,
    MessageRepository,
    VisitorAlreadyClaimed,
    VisitorRepository,
)
from visitor_lounge.scheduler import (
    QueueFull,
    SchedulerShuttingDown,
    VisitorAlreadyQueued,
)
from visitor_lounge.security import (
    InvalidVisitorName,
    RateLimitExceeded,
    normalize_visitor_name,
)
from visitor_lounge.turn_coordinator import (
    VisitorTurnCoordinator,
    VisitorTurnQueueFull,
)
from visitor_lounge.visitor_service import (
    LoungeClosed,
    SensitiveCredentialInput,
    VisitorNotClaimed,
    VisitorSafetyLocked,
    VisitorService,
    VisitorUnavailable,
)


MAX_MCP_INPUT_CHARS = 500
MAX_MCP_OUTPUT_CHARS = 800
MAX_REQUEST_ID_CHARS = 128


class McpLoungeService:
    """Return stable dictionaries without exposing transport or secret details."""

    def __init__(
        self,
        container: Container,
        visitor_service: VisitorService,
        coordinator: VisitorTurnCoordinator,
    ) -> None:
        self.container = container
        self.visitor_service = visitor_service
        self.coordinator = coordinator
        self.visitors = VisitorRepository(container.database)
        self.messages = MessageRepository(container.database)
        self.background = BackgroundRepository(container.database)
        self.reception = ReceptionSettingsRepository(
            container.database, container.settings.root
        )
        self.home_context = visitor_service.home_context
        self._visit_start_after: dict[str, str | None] = {}

    def get_lounge_info(self, visitor_id: str) -> dict[str, object]:
        visitor = self.visitor_service.effective_visitor(visitor_id)
        reception = self.reception.get()
        return {
            "status": "ok",
            "host_name": self.container.settings.host_display_name,
            "lounge_state": self._lounge_state(visitor, reception.lounge_enabled),
            "identity_claimed": visitor.display_name is not None,
            "visitor_name": visitor.display_name,
            "visitor_kind": visitor.visitor_kind,
            "required_next_tool": (
                "claim_identity" if visitor.display_name is None else "begin_visit"
            ),
            "max_input_chars": MAX_MCP_INPUT_CHARS,
            "max_output_chars": MAX_MCP_OUTPUT_CHARS,
            "accepted_content": ["text"],
            "recording_disclosure": self.container.settings.recording_disclosure,
            "recording_disclosure_version": (
                self.container.settings.recording_disclosure_version
            ),
            "safety_locked_until": self._timestamp(visitor.safety_locked_until),
        }

    def claim_identity(
        self, visitor_id: str, name: str, consent: bool
    ) -> dict[str, object]:
        visitor = self.visitor_service.effective_visitor(visitor_id)
        if visitor.display_name is not None:
            return self._already_claimed(visitor)
        unavailable = self._unavailable(visitor)
        if unavailable is not None:
            return unavailable
        if not consent:
            return {
                "status": "consent_required",
                "message": "需要同意来访记录说明后才能固定身份。",
            }
        if not isinstance(name, str):
            return {"status": "invalid_name", "message": "名字必须是纯文本。"}
        try:
            normalized = normalize_visitor_name(name, set())
        except InvalidVisitorName:
            return {
                "status": "invalid_name",
                "limit": 200,
                "message": "名字需要包含 1 至 200 个有效字符。",
            }
        reception = self.reception.get()
        welcome = reception.first_welcome.replace(
            "{访客名字}", normalized
        ).replace("{接待人名字}", self.container.settings.host_display_name)
        try:
            self.visitors.claim_name(
                visitor_id,
                normalized,
                self.container.settings.recording_disclosure_version,
                greeting=welcome,
                greeting_at=self.container.clock(),
                source="mcp",
            )
        except VisitorAlreadyClaimed:
            return self._already_claimed(self.visitors.visitor(visitor_id))
        claimed = self.visitors.visitor(visitor_id)
        return {
            "status": "claimed",
            "visitor_name": claimed.display_name,
            "visitor_kind": claimed.visitor_kind,
            "message": "身份已经固定；如需修改名字，请联系管理员。",
        }

    def begin_visit(self, visitor_id: str) -> dict[str, object]:
        visitor = self.visitor_service.effective_visitor(visitor_id)
        if visitor.display_name is None:
            return self._identity_unclaimed()
        if not self.reception.get().lounge_enabled:
            return {"status": "lounge_closed"}
        unavailable = self._unavailable(visitor)
        if unavailable is not None:
            return unavailable
        try:
            visitor = self.visitor_service.begin_visit(visitor_id, source="mcp")
        except VisitorSafetyLocked as error:
            return self._locked(error.until)
        except VisitorUnavailable:
            return self._availability_for(visitor_id)
        latest = self.messages.timeline(visitor_id, limit=1)
        self._visit_start_after[visitor_id] = latest[-1].id if latest else None
        return {
            "status": "ok",
            "visit_status": "active",
            "visitor_name": visitor.display_name,
            "visitor_kind": visitor.visitor_kind,
            **self._state_fields(visitor_id, after_message_id=None),
        }

    async def talk_to_host(
        self,
        visitor_id: str,
        message: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(message, str):
            return {"status": "invalid_message", "message": "只接受纯文本。"}
        received = len(message)
        if received > MAX_MCP_INPUT_CHARS:
            return {
                "status": "message_too_long",
                "limit": MAX_MCP_INPUT_CHARS,
                "received": received,
                "message": "本次输入超过 500 字，请压缩或拆分后重新发送。",
            }
        if request_id is None or (
            isinstance(request_id, str) and not request_id.strip()
        ):
            request_id = str(uuid4())
        if (
            not isinstance(request_id, str)
            or len(request_id) > MAX_REQUEST_ID_CHARS
        ):
            return {
                "status": "invalid_request_id",
                "message": "request_id 需要包含 1 至 128 个字符。",
            }
        visitor = self.visitor_service.effective_visitor(visitor_id)
        if visitor.display_name is None:
            return self._identity_unclaimed()
        unavailable = self._unavailable(visitor)
        if unavailable is not None:
            return unavailable
        if visitor.status == "suspended":
            begin = self.begin_visit(visitor_id)
            if begin.get("status") != "ok":
                return begin
        try:
            ticket = await self.coordinator.submit(
                visitor_id,
                request_id,
                message,
                source="mcp",
            )
            job = await self.visitor_service.wait_for_job(ticket.job_id, visitor_id)
        except VisitorNotClaimed:
            return self._identity_unclaimed()
        except LoungeClosed:
            return {"status": "lounge_closed"}
        except VisitorSafetyLocked as error:
            return self._locked(error.until)
        except VisitorUnavailable:
            return self._availability_for(visitor_id)
        except QuotaExhausted as error:
            return {
                "status": "quota_exhausted",
                "reset_at": error.ends_at.isoformat(),
            }
        except SensitiveCredentialInput:
            return {
                "status": "credential_rejected",
                "message": self.reception.get().credential_detected,
            }
        except VisitorTurnQueueFull:
            return {"status": "visitor_busy", "max_waiting": 3}
        except (VisitorAlreadyQueued, VisitorHasPendingJob, RateLimitExceeded):
            return {"status": "visitor_busy"}
        except (QueueFull, SchedulerShuttingDown):
            return {"status": "service_busy"}
        except RequestConflict:
            return {"status": "request_conflict", "request_id": request_id}
        except VisitorInputTooLong:
            return {
                "status": "message_too_long",
                "limit": MAX_MCP_INPUT_CHARS,
                "received": received,
            }
        except PromptBudgetExceeded:
            return {"status": "invalid_message"}
        if job.status != "completed" or job.response_message_id is None:
            return {
                "status": "generation_failed",
                "request_id": request_id,
                "visitor_message_id": job.message_id,
                **self._quota_fields(visitor_id),
            }
        current = self.visitor_service.effective_visitor(visitor_id)
        return {
            "status": "ok",
            "request_id": request_id,
            "reply": job.visible_text,
            "action": job.action or "continue",
            "visit_status": current.status,
            "visitor_message_id": job.message_id,
            "host_message_id": job.response_message_id,
            "received_at": job.created_at.isoformat(),
            "completed_at": self._timestamp(job.finished_at),
            **self._quota_fields(visitor_id),
        }

    def get_visit_state(
        self, visitor_id: str, after_message_id: str | None = None
    ) -> dict[str, object]:
        visitor = self.visitor_service.effective_visitor(visitor_id)
        return {
            "status": "ok",
            "visitor_name": visitor.display_name,
            "visitor_kind": visitor.visitor_kind,
            "visit_status": visitor.status,
            "safety_locked_until": self._timestamp(visitor.safety_locked_until),
            **self._state_fields(visitor_id, after_message_id=after_message_id),
        }

    async def end_visit(
        self,
        visitor_id: str,
        final_message: str | None = None,
    ) -> dict[str, object]:
        if final_message is not None:
            if not isinstance(final_message, str):
                return {"status": "invalid_message", "message": "只接受纯文本。"}
            received = len(final_message)
            if received > MAX_MCP_INPUT_CHARS:
                return {
                    "status": "message_too_long",
                    "limit": MAX_MCP_INPUT_CHARS,
                    "received": received,
                    "message": "本次输入超过 500 字，请压缩后重新发送。",
                }
        visitor = self.visitor_service.effective_visitor(visitor_id)
        if visitor.display_name is None:
            return self._identity_unclaimed()
        unavailable = self._unavailable(visitor)
        if unavailable is not None:
            return unavailable
        if final_message is not None and final_message.strip():
            self.messages.append_message(
                visitor_id,
                "visitor",
                final_message,
                source="mcp",
            )
        start_after = self._visit_start_after.pop(visitor_id, None)
        visit_messages = self.messages.timeline(
            visitor_id, after_message_id=start_after, limit=16
        )
        try:
            self.visitor_service.end_visit(visitor_id)
        except VisitorSafetyLocked as error:
            return self._locked(error.until)
        except VisitorUnavailable:
            return self._availability_for(visitor_id)
        payload = [
            {
                "direction": "inbound" if message.sender == "visitor" else "outbound",
                "content": message.content[:500],
            }
            for message in visit_messages
        ]
        turn_count = sum(1 for message in visit_messages if message.sender == "host")
        asyncio.create_task(
            self.home_context.publish_reception_report(
                visitor.display_name,
                payload,
                turn_count=turn_count,
            )
        )
        return {
            "status": "ok",
            "visit_status": "ended",
            "visitor_name": visitor.display_name,
        }

    def _state_fields(
        self, visitor_id: str, after_message_id: str | None
    ) -> dict[str, object]:
        messages = self.messages.timeline(
            visitor_id, after_message_id=after_message_id, limit=30
        )
        latest = self.visitors.latest_job(visitor_id)
        memory_available, memory_updated_at = self.background.memory_status(visitor_id)
        return {
            "messages": [self._message_payload(message) for message in messages],
            "next_after_message_id": messages[-1].id if messages else after_message_id,
            "job": None if latest is None else self._job_payload(latest),
            "memory_available": memory_available,
            "memory_updated_at": self._timestamp(memory_updated_at),
            **self._quota_fields(visitor_id),
        }

    def _quota_fields(self, visitor_id: str) -> dict[str, object]:
        try:
            quota = self.visitor_service.quota.state(
                visitor_id, self.container.clock()
            )
        except ValueError:
            limit = self.visitor_service.quota.configured_limit()
            return {
                "quota_limit": limit,
                "quota_used": 0,
                "quota_reserved": 0,
                "quota_remaining": limit,
                "quota_reset_at": None,
            }
        return self._quota_payload(quota)

    @staticmethod
    def _quota_payload(quota: QuotaState) -> dict[str, object]:
        return {
            "quota_limit": quota.limit,
            "quota_used": quota.used,
            "quota_reserved": quota.reserved,
            "quota_remaining": max(0, quota.limit - quota.used - quota.reserved),
            "quota_reset_at": quota.ends_at.isoformat(),
        }

    @staticmethod
    def _message_payload(message: Message) -> dict[str, object]:
        return {
            "id": message.id,
            "sender": message.sender,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
            "source": message.source,
            "delivery_status": message.delivery_status,
        }

    @staticmethod
    def _job_payload(job: GenerationJobRecord) -> dict[str, object]:
        return {
            "job_id": job.id,
            "request_id": job.request_id,
            "status": job.status,
            "action": job.action,
            "visible_text": job.visible_text,
            "created_at": job.created_at.isoformat(),
            "finished_at": McpLoungeService._timestamp(job.finished_at),
        }

    def _availability_for(self, visitor_id: str) -> dict[str, object]:
        return self._unavailable(self.visitor_service.effective_visitor(visitor_id)) or {
            "status": "service_busy"
        }

    @staticmethod
    def _unavailable(visitor: VisitorRecord) -> dict[str, object] | None:
        if visitor.status == "safety_lock":
            return McpLoungeService._locked(visitor.safety_locked_until)
        if visitor.status == "paused":
            return {"status": "visitor_paused"}
        return None

    @staticmethod
    def _locked(until: datetime | None) -> dict[str, object]:
        return {
            "status": "visitor_locked",
            "locked_until": McpLoungeService._timestamp(until),
        }

    @staticmethod
    def _already_claimed(visitor: VisitorRecord) -> dict[str, object]:
        return {
            "status": "already_claimed",
            "visitor_name": visitor.display_name,
            "visitor_kind": visitor.visitor_kind,
            "message": "名字已经固定，如需修改请联系管理员。",
        }

    @staticmethod
    def _identity_unclaimed() -> dict[str, object]:
        return {
            "status": "identity_unclaimed",
            "next_tool": "claim_identity",
        }

    @staticmethod
    def _lounge_state(visitor: VisitorRecord, enabled: bool) -> str:
        if not enabled:
            return "closed"
        if visitor.status == "safety_lock":
            return "locked"
        if visitor.status == "paused":
            return "paused"
        return "open"

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()
