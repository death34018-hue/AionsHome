"""Authenticated visitor chat orchestration for the standalone lounge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
import json
from uuid import uuid4

from visitor_lounge.content_safety import detect_credential_category
from visitor_lounge.container import Container
from visitor_lounge.home_context_bridge import HomeContextBridge
from visitor_lounge.models import (
    GenerationRequest,
    GenerationJobRecord,
    JobEvent,
    MessageSource,
    QuotaState,
    VisitorRecord,
)
from visitor_lounge.prompts import (
    MAX_HISTORY_MESSAGES,
    PromptBuilder,
    validate_visitor_input,
)
from visitor_lounge.quota import QuotaService, RequestConflict
from visitor_lounge.reception_settings import ReceptionSettingsRepository
from visitor_lounge.repository import (
    BackgroundRepository,
    MessageRepository,
    VisitorRepository,
)
from visitor_lounge.scheduler import GenerationScheduler, QueueTicket, ResourceGate
from visitor_lounge.security import AttemptLimiters, RateLimitExceeded


class VisitorUnavailable(PermissionError):
    """Raised when the authenticated identity may no longer send messages."""


class VisitorSafetyLocked(VisitorUnavailable):
    """Raised when an identity is still inside its safety cooldown."""

    def __init__(self, until: datetime) -> None:
        super().__init__(f"visitor safety locked until {until.isoformat()}")
        self.until = until


class VisitorNotClaimed(PermissionError):
    """Raised when a visitor has not completed first-login claiming."""


class LoungeClosed(PermissionError):
    """Raised when the owner has paused new visitor messages."""


class SensitiveCredentialInput(ValueError):
    """Raised without retaining credential-shaped visitor input."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class JobAccessDenied(LookupError):
    """Raised without revealing whether another visitor's job exists."""


@dataclass(frozen=True)
class QueueTicketSnapshot:
    job_id: str
    position: int | None


class _PromptGenerationAdapter:
    def __init__(self, adapter: object) -> None:
        self._adapter = adapter

    def generate(self, request: GenerationRequest):
        return self._adapter.generate(request.prompt)  # type: ignore[attr-defined,no-any-return]


class VisitorService:
    """Validate, reserve, prompt, schedule, and expose visitor-owned state."""

    def __init__(self, container: Container) -> None:
        if container.codex_adapter is None:
            raise ValueError("a generation adapter is required")
        self.container = container
        self.repository = VisitorRepository(container.database)
        self.background = BackgroundRepository(container.database)
        self.messages = MessageRepository(container.database)
        self.reception = ReceptionSettingsRepository(
            container.database, container.settings.root
        )
        self.quota = QuotaService(
            container.database,
            limit_provider=lambda: self.reception.get().hourly_quota_limit,
        )
        self._message_limiter = AttemptLimiters().message
        persona_path = container.settings.root / "config" / "persona.md"
        persona_text = persona_path.read_text("utf-8")
        self.prompt_builder = PromptBuilder(
            persona_text=persona_text,
            host_display_name=container.settings.host_display_name,
        )
        self.home_context = HomeContextBridge(
            container.settings.root.parent
            / "aion-chat"
            / "data"
            / "lounge-context-bridge.key"
        )
        resource_gate = None
        if container.resource_sampler is not None:
            resource_gate = ResourceGate(sampler=container.resource_sampler)
        self.scheduler = GenerationScheduler(
            database=container.database,
            quota=self.quota,
            adapter=_PromptGenerationAdapter(container.codex_adapter),
            settings=container.settings,
            resource_gate=resource_gate,
            clock=container.clock,
            safety_template_provider=lambda: self.reception.get().unsafe_request,
        )

    async def start(self) -> None:
        await self.scheduler.start()

    async def shutdown(self) -> None:
        await self.scheduler.shutdown()

    async def send(
        self,
        visitor_id: str,
        request_id: str,
        text: str,
        source: MessageSource = "web",
    ) -> QueueTicket | QueueTicketSnapshot:
        try:
            existing_job = self.repository.job(request_id)
        except KeyError:
            existing_job = None
        if existing_job is not None:
            if existing_job.visitor_id != visitor_id:
                raise RequestConflict(request_id)
            return QueueTicketSnapshot(
                job_id=existing_job.id,
                position=self.scheduler.queue_position(existing_job.id),
            )

        reception = self.reception.get()
        if not reception.lounge_enabled:
            raise LoungeClosed()
        validated = validate_visitor_input(text)
        visitor = self.effective_visitor(visitor_id)
        if (
            visitor.status == "safety_lock"
            and visitor.safety_locked_until is not None
            and visitor.safety_locked_until > self.container.clock()
        ):
            raise VisitorSafetyLocked(visitor.safety_locked_until)
        if visitor.status != "active":
            raise VisitorUnavailable()
        if visitor.display_name is None:
            raise VisitorNotClaimed()
        self.background.record_activity(visitor_id, self.container.clock())
        if not self._message_limiter.allow(visitor_id):
            raise RateLimitExceeded("message rate limited")
        credential_category = detect_credential_category(validated)
        if credential_category is not None:
            self._record_credential_rejection(visitor_id, credential_category)
            raise SensitiveCredentialInput(credential_category)

        reservation = self.quota.reserve_message(
            visitor_id,
            request_id,
            validated,
            self.container.clock(),
            source=source,
        )
        job = self.repository.job(request_id)
        history = [
            message
            for message in self.messages.recent(
                visitor_id, limit=MAX_HISTORY_MESSAGES + 1
            )
            if message.id != job.message_id
        ][-MAX_HISTORY_MESSAGES:]
        try:
            trusted_home_context = await self.home_context.fetch(
                validated,
                [
                    {
                        "direction": "inbound" if item.sender == "visitor" else "outbound",
                        "content": item.content[:500],
                    }
                    for item in history[-6:]
                ],
            )
            prompt_builder = PromptBuilder(
                persona_text=reception.persona_text,
                host_display_name=self.container.settings.host_display_name,
            )
            prompt = prompt_builder.chat(
                visitor.display_name,
                validated,
                history,
                self.background.recent_summaries(visitor_id, limit=1),
                self.quota.state(visitor_id),
                trusted_home_context=trusted_home_context,
            )
            request = GenerationRequest(
                job_id=reservation.job_id,
                request_id=request_id,
                visitor_id=visitor_id,
                message_id=job.message_id or "",
                prompt=prompt,
                source=source,
            )
            return await self.scheduler.submit(request)
        except BaseException:
            self.quota.cancel_unsubmitted(visitor_id, request_id)
            raise

    async def wait_for_job(
        self, job_id: str, visitor_id: str
    ) -> GenerationJobRecord:
        self.assert_job_owner(job_id, visitor_id)
        try:
            ticket = self.scheduler.ticket(job_id)
        except KeyError:
            return self.repository.job_by_id(job_id)
        await ticket.final()
        return self.assert_job_owner(job_id, visitor_id)

    def assert_job_owner(self, job_id: str, visitor_id: str):
        try:
            job = self.repository.job_by_id(job_id)
        except KeyError as error:
            raise JobAccessDenied() from error
        if job.visitor_id != visitor_id:
            raise JobAccessDenied()
        return job

    async def sse_events(
        self, job_id: str, visitor_id: str, *, after_event_id: int = 0
    ) -> AsyncIterator[str]:
        self.assert_job_owner(job_id, visitor_id)
        try:
            async for event in self.scheduler.events(
                job_id, after_event_id=after_event_id
            ):
                yield self._encode_sse(event)
        except KeyError:
            job = self.assert_job_owner(job_id, visitor_id)
            yield self._encode_sse(
                JobEvent(
                    "snapshot",
                    {
                        "job_id": job.id,
                        "state": job.status,
                        "visible_text": job.visible_text,
                    },
                    event_id=max(after_event_id + 1, 1),
                )
            )
            terminal = "completed" if job.status == "completed" else "failed"
            yield self._encode_sse(
                JobEvent(
                    terminal,
                    {
                        "state": job.status,
                        "action": job.action or "continue",
                        "usage": (
                            {
                                "input_tokens": job.input_tokens,
                                "output_tokens": job.output_tokens,
                            }
                            if job.usage_reported
                            else {}
                        ),
                        "usage_reported": job.usage_reported,
                    },
                    event_id=max(after_event_id + 2, 2),
                )
            )

    def state(self, visitor_id: str) -> dict[str, object]:
        visitor = self.effective_visitor(visitor_id)
        recent = self.messages.timeline(visitor_id, limit=MAX_HISTORY_MESSAGES)
        try:
            quota = self.quota.state(visitor_id, self.container.clock())
            if quota.ends_at <= self.container.clock():
                limit = self.quota.configured_limit()
                quota_payload = {
                    "limit": limit,
                    "used": 0,
                    "reserved": 0,
                    "remaining": limit,
                    "reset_at": None,
                }
            else:
                quota_payload = self._quota_payload(quota)
        except ValueError:
            limit = self.quota.configured_limit()
            quota_payload = {
                "limit": limit,
                "used": 0,
                "reserved": 0,
                "remaining": limit,
                "reset_at": None,
            }
        job = self.repository.latest_job(visitor_id)
        job_payload: dict[str, object] | None = None
        if job is not None:
            job_payload = {
                "job_id": job.id,
                "status": "generating" if job.status == "running" else job.status,
                "queue_position": self.scheduler.queue_position(job.id),
                "visible_text": job.visible_text,
                "action": job.action,
            }
        return {
            "host_name": self.container.settings.host_display_name,
            "standby_name": self.container.settings.standby_display_name,
            "host_avatar": "/static/avatars/host.jpg",
            "standby_avatar": "/static/avatars/standby.png",
            "visitor_name": visitor.display_name,
            "visitor_status": visitor.status,
            "visitor_kind": visitor.visitor_kind,
            "safety_locked_until": (
                None
                if visitor.safety_locked_until is None
                else visitor.safety_locked_until.isoformat()
            ),
            "quota": quota_payload,
            "messages": [
                {
                    "id": message.id,
                    "sender": message.sender,
                    "content": message.content,
                    "created_at": message.created_at.isoformat(),
                    "source": message.source,
                    "delivery_status": message.delivery_status,
                }
                for message in recent
            ],
            "job": job_payload,
        }

    def record_activity(self, visitor_id: str) -> None:
        self.effective_visitor(visitor_id)
        self.background.record_activity(visitor_id, self.container.clock())

    def record_login(
        self, visitor_id: str, source: MessageSource = "web"
    ) -> None:
        self.effective_visitor(visitor_id)
        self.background.record_login(
            visitor_id,
            self.container.clock(),
            returning_greeting=self.reception.get().returning_welcome,
            source=source,
        )

    def effective_visitor(self, visitor_id: str) -> VisitorRecord:
        self.repository.release_expired_safety_lock(
            visitor_id, self.container.clock()
        )
        return self.repository.visitor(visitor_id)

    def begin_visit(
        self, visitor_id: str, source: MessageSource = "web"
    ) -> VisitorRecord:
        visitor = self.effective_visitor(visitor_id)
        if visitor.status == "safety_lock":
            if visitor.safety_locked_until is not None:
                raise VisitorSafetyLocked(visitor.safety_locked_until)
            raise VisitorUnavailable()
        if visitor.status == "paused":
            raise VisitorUnavailable()
        self.background.record_login(
            visitor_id,
            self.container.clock(),
            returning_greeting=self.reception.get().returning_welcome,
            source=source,
        )
        return self.effective_visitor(visitor_id)

    def end_visit(self, visitor_id: str) -> None:
        visitor = self.effective_visitor(visitor_id)
        if visitor.status == "safety_lock":
            if visitor.safety_locked_until is not None:
                raise VisitorSafetyLocked(visitor.safety_locked_until)
            raise VisitorUnavailable()
        if visitor.status == "paused":
            raise VisitorUnavailable()
        self.repository.end_visit(visitor_id, self.container.clock())

    def _record_credential_rejection(self, visitor_id: str, category: str) -> None:
        payload = json.dumps(
            {
                "actor": "visitor_precheck",
                "details": {"category": category},
            },
            separators=(",", ":"),
        )
        with self.container.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO audit_events (id, visitor_id, kind, payload, created_at)
                VALUES (?, ?, 'credential_input_rejected', ?, ?)
                """,
                (
                    str(uuid4()),
                    visitor_id,
                    payload,
                    self.container.clock().isoformat(),
                ),
            )

    @staticmethod
    def _quota_payload(quota: QuotaState) -> dict[str, object]:
        return {
            "limit": quota.limit,
            "used": quota.used,
            "reserved": quota.reserved,
            "remaining": max(0, quota.limit - quota.used - quota.reserved),
            "reset_at": quota.ends_at.isoformat(),
        }

    @staticmethod
    def _encode_sse(event: JobEvent) -> str:
        payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))
        event_id = f"id: {event.event_id}\n" if event.event_id > 0 else ""
        return f"{event_id}event: {event.kind}\ndata: {payload}\n\n"
