"""Strict outbound orchestration for Visitor Lounge friend visits."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Literal

from lounge_friends import LoungeFriend, LoungeFriendStore, redact_visitor_key
from lounge_visit_repository import LoungeVisitRepository
from lounge_visit_tasks import lounge_visit_tasks
from mcp_client import MCPManager, MCPToolProtocolError, MCPToolTransportError


ComposeNextMessage = Callable[
    [str, LoungeFriend, list[dict], str, int], Awaitable[str]
]


@dataclass(frozen=True)
class LoungeVisitResult:
    visit_id: str
    status: Literal["completed", "interrupted", "rejected"]
    turn_count: int
    final_reply: str
    reason: str


_OUTBOUND_VISIT_LOCK = asyncio.Lock()
_TOTAL_TIMEOUT_SECONDS = 600
_TOOL_TIMEOUT_SECONDS = 120
_DISCONNECT_TIMEOUT_SECONDS = 5
_MAX_MESSAGE_CHARS = 500
_MAX_TURNS = 8
_MAX_TIMELINE_ENTRIES = 20
_APPROVED_TOOLS = frozenset(
    {
        "get_lounge_info",
        "claim_identity",
        "begin_visit",
        "talk_to_host",
        "get_visit_state",
        "end_visit",
    }
)
_REJECTED_REMOTE_STATUSES = frozenset(
    {
        "visitor_locked",
        "visitor_paused",
        "quota_exhausted",
        "lounge_closed",
        "identity_unclaimed",
        "consent_required",
        "invalid_name",
        "credential_rejected",
        "message_too_long",
        "invalid_message",
        "invalid_request_id",
    }
)
_INTERRUPTED_REMOTE_STATUSES = frozenset(
    {
        "generation_failed",
        "prompt_budget_exceeded",
        "visitor_busy",
        "service_busy",
        "request_conflict",
    }
)
_TERMINAL_REASON_CODES = frozenset(
    {
        "network_reconnect_failed",
        "request_timeout",
        "generation_failed_after_retries",
        "prompt_budget_exceeded",
        "response_too_long",
        "lounge_closed",
        "quota_exhausted",
        "user_cancelled",
        "service_restarted",
        "repository_failed",
        "remote_protocol_error",
        "unexpected_failure",
        "visitor_locked",
        "visitor_paused",
        "visitor_busy",
        "service_busy",
        "request_conflict",
        "friend_not_found",
        "local_state_failed",
        "invalid_trigger_source",
        "invalid_topic",
        "friend_disabled",
        "identity_name_unavailable",
        "unsupported_server",
        "invalid_message",
        "message_too_long",
        "identity_unclaimed",
        "consent_required",
        "invalid_name",
        "credential_rejected",
        "invalid_request_id",
    }
)
_SAFE_TIMELINE_FIELDS = (
    "id",
    "sender",
    "content",
    "created_at",
    "source",
    "delivery_status",
)
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>'\"]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SECRET_RE = re.compile(
    r"(?i)\b(?:visitor[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"oauth[_ -]?token|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_LOCAL_ACTION_RE = re.compile(
    r"\s*<<LOUNGE_VISIT_ACTION:(continue|closing|end)>>\s*$"
)


class _VisitStopped(Exception):
    def __init__(
        self,
        status: Literal["interrupted", "rejected"],
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _consume_late_task(task: asyncio.Task) -> None:
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:
            pass


async def _await_hard_deadline(awaitable, timeout: float):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_consume_late_task)
    raise TimeoutError()


class LoungeVisitCoordinator:
    def __init__(
        self,
        friend_store: LoungeFriendStore,
        repository: LoungeVisitRepository,
        mcp_manager: MCPManager,
        actor_name_resolver: Callable[[str], str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.friend_store = friend_store
        self.repository = repository
        self.mcp_manager = mcp_manager
        self.actor_name_resolver = actor_name_resolver
        self.clock = clock

    async def run_visit(
        self,
        actor_id: str,
        friend_id: str,
        trigger_source: Literal["manual", "chat", "autonomy"],
        topic: str,
        compose_next: ComposeNextMessage,
    ) -> LoungeVisitResult:
        task = asyncio.current_task()
        if task is not None:
            lounge_visit_tasks.register(actor_id, task)
        try:
            return await self._run_registered_visit(
                actor_id, friend_id, trigger_source, topic, compose_next
            )
        finally:
            if task is not None:
                lounge_visit_tasks.unregister(actor_id, task)

    async def _run_registered_visit(
        self,
        actor_id: str,
        friend_id: str,
        trigger_source: Literal["manual", "chat", "autonomy"],
        topic: str,
        compose_next: ComposeNextMessage,
    ) -> LoungeVisitResult:
        progress = {"visit_id": "", "turn_count": 0, "final_reply": ""}
        async with _OUTBOUND_VISIT_LOCK:
            try:
                cancel_deadline = (
                    asyncio.get_running_loop().time() + _TOTAL_TIMEOUT_SECONDS
                )
                async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                    return await self._run_locked(
                        actor_id,
                        friend_id,
                        trigger_source,
                        topic,
                        compose_next,
                        progress,
                        cancel_deadline,
                    )
            except TimeoutError:
                return await self._finish_without_details(
                    str(progress["visit_id"]),
                    "interrupted",
                    int(progress["turn_count"]),
                    "request_timeout",
                    str(progress["final_reply"]),
                )

    async def _run_locked(
        self,
        actor_id: str,
        friend_id: str,
        trigger_source: str,
        topic: str,
        compose_next: ComposeNextMessage,
        progress: dict[str, object],
        cancel_deadline: float,
    ) -> LoungeVisitResult:
        try:
            friend = self.friend_store.get_owned(actor_id, friend_id)
        except KeyError:
            return LoungeVisitResult("", "rejected", 0, "", "friend_not_found")
        except Exception:
            return LoungeVisitResult("", "interrupted", 0, "", "local_state_failed")

        safe_topic = (
            redact_visitor_key(topic, friend.visitor_key)
            if isinstance(topic, str)
            else ""
        )
        try:
            visit_id = await self.repository.start(
                actor_id, friend_id, trigger_source, safe_topic
            )
            progress["visit_id"] = visit_id
        except Exception:
            return LoungeVisitResult("", "interrupted", 0, "", "repository_failed")

        if trigger_source not in {"manual", "chat", "autonomy"}:
            return await self._finish_without_details(
                visit_id, "rejected", 0, "invalid_trigger_source"
            )
        if not isinstance(topic, str):
            return await self._finish_without_details(
                visit_id, "rejected", 0, "invalid_topic"
            )
        if not friend.enabled:
            return await self._finish_without_details(
                visit_id, "rejected", 0, "friend_disabled"
            )

        connection_id = f"visitor-lounge:{actor_id}:{friend_id}"
        turn_count = 0
        final_reply = ""
        status: Literal["completed", "interrupted", "rejected"] = "interrupted"
        reason = "unexpected_failure"
        visit_begun = False
        end_attempted = False
        locally_settled = False

        try:
            tools = await self._connect_with_one_retry(connection_id, friend)
            self._require_approved_protocol(tools)

            lounge_info = await self._call_tool(
                connection_id, "get_lounge_info", {}
            )
            self._require_status(lounge_info, {"ok"})

            if lounge_info.get("identity_claimed") is not True:
                actor_name = self.actor_name_resolver(actor_id)
                if not isinstance(actor_name, str) or not actor_name.strip():
                    raise _VisitStopped("rejected", "identity_name_unavailable")
                claim = await self._call_tool(
                    connection_id,
                    "claim_identity",
                    {"name": actor_name.strip(), "consent": True},
                )
                self._require_status(claim, {"claimed", "already_claimed"})

            begun = await self._call_tool(connection_id, "begin_visit", {})
            self._require_status(begun, {"ok"})
            visit_begun = True
            timeline = self._pure_text_timeline(begun.get("messages"), friend)

            try:
                self.friend_store.mark_visited(
                    actor_id, friend_id, float(self.clock())
                )
            except Exception:
                raise _VisitStopped("interrupted", "local_state_failed")

            prompt_friend = self._prompt_safe_friend(friend)
            max_turns = min(max(friend.max_turns, 1), _MAX_TURNS)
            reconnect_used = False
            for turn in range(1, max_turns + 1):
                try:
                    composed = await compose_next(
                        actor_id,
                        prompt_friend,
                        list(timeline),
                        self._safe_prompt_text(safe_topic),
                        turn,
                    )
                except Exception:
                    raise _VisitStopped(
                        "interrupted", "generation_failed_after_retries"
                    )
                message, local_action = self._parse_composed_message(composed)
                self._validate_outbound_message(message)

                request_id = str(uuid.uuid4())
                try:
                    await self.repository.append_message(
                        visit_id, "outbound", message
                    )
                except Exception:
                    raise _VisitStopped("interrupted", "repository_failed")

                response, reconnected = await self._talk_with_one_reconnect(
                    connection_id,
                    friend,
                    message,
                    request_id,
                    allow_reconnect=not reconnect_used,
                )
                reconnect_used = reconnect_used or reconnected
                self._require_status(response, {"ok"})
                reply = response.get("reply")
                if not isinstance(reply, str):
                    raise _VisitStopped("interrupted", "remote_protocol_error")
                action = response.get("action", "continue")
                if action not in {"continue", "closing", "end"}:
                    raise _VisitStopped("interrupted", "remote_protocol_error")

                final_reply = self._safe_remote_text(reply, friend)
                progress["final_reply"] = final_reply
                remote_message_id = response.get("host_message_id", "")
                if not isinstance(remote_message_id, str):
                    remote_message_id = ""
                remote_message_id = self._safe_remote_text(
                    remote_message_id, friend
                )
                try:
                    await self.repository.append_message(
                        visit_id,
                        "inbound",
                        final_reply,
                        remote_message_id=remote_message_id,
                    )
                    turn_count += 1
                    progress["turn_count"] = turn_count
                    await self.repository.update_progress(visit_id, turn_count)
                except Exception:
                    raise _VisitStopped("interrupted", "repository_failed")

                timeline = self._extend_timeline(
                    timeline, message, final_reply
                )
                if action == "end":
                    reason = "action_end"
                    break
                if action == "closing":
                    closing_timeline = list(timeline) + [
                        {"_lounge_control": "reply_and_end"}
                    ]
                    try:
                        composed_final = await compose_next(
                            actor_id,
                            prompt_friend,
                            closing_timeline,
                            self._safe_prompt_text(safe_topic),
                            turn + 1,
                        )
                    except Exception:
                        raise _VisitStopped(
                            "interrupted", "generation_failed_after_retries"
                        )
                    final_message, _ = self._parse_composed_message(
                        composed_final
                    )
                    self._validate_outbound_message(final_message)
                    try:
                        await self.repository.append_message(
                            visit_id, "outbound", final_message
                        )
                    except Exception:
                        raise _VisitStopped("interrupted", "repository_failed")
                    end_attempted = True
                    ended = await self._end_with_one_reconnect(
                        connection_id,
                        friend,
                        {
                            "final_message": final_message,
                            "status": "completed",
                        },
                    )
                    self._require_status(ended, {"ok"})
                    reason = "action_end"
                    status = "completed"
                    break
            else:
                reason = "max_turns"

            if not end_attempted:
                end_attempted = True
                ended = await self._end_with_one_reconnect(
                    connection_id,
                    friend,
                    {"status": "completed"},
                )
                self._require_status(ended, {"ok"})
                status = "completed"
        except asyncio.CancelledError:
            status = "interrupted"
            reason = (
                "request_timeout"
                if asyncio.get_running_loop().time() >= cancel_deadline
                else "user_cancelled"
            )
        except _VisitStopped as stopped:
            status = "interrupted" if visit_begun else stopped.status
            reason = stopped.reason
        except Exception:
            status = "interrupted"
            reason = "unexpected_failure"
        finally:
            if status != "completed":
                try:
                    await self.repository.finish(
                        visit_id,
                        status,
                        turn_count,
                        error=reason,
                    )
                    locally_settled = True
                except Exception:
                    pass
            if visit_begun and not end_attempted:
                end_attempted = True
                try:
                    await self._end_with_one_reconnect(
                        connection_id,
                        friend,
                        {
                            "status": "interrupted",
                            "reason": self._remote_terminal_reason(reason),
                        },
                    )
                except BaseException:
                    pass
            try:
                await self._disconnect_bounded(connection_id)
            except Exception:
                pass

        if not locally_settled:
            try:
                await self.repository.finish(
                    visit_id,
                    status,
                    turn_count,
                    error="" if status == "completed" else reason,
                )
            except Exception:
                return LoungeVisitResult(
                    visit_id, "interrupted", turn_count, final_reply, "repository_failed"
                )
        return LoungeVisitResult(
            visit_id, status, turn_count, final_reply, reason
        )

    async def _talk_with_one_reconnect(
        self,
        connection_id: str,
        friend: LoungeFriend,
        message: str,
        request_id: str,
        *,
        allow_reconnect: bool,
    ) -> tuple[dict[str, object], bool]:
        arguments = {"message": message, "request_id": request_id}
        try:
            return (
                await self._call_tool(
                    connection_id, "talk_to_host", arguments
                ),
                False,
            )
        except _VisitStopped:
            raise
        except (MCPToolTransportError, TimeoutError):
            if not allow_reconnect:
                raise _VisitStopped(
                    "interrupted", "network_reconnect_failed"
                )
            try:
                await self._disconnect_bounded(connection_id)
            except BaseException:
                pass
            try:
                tools = await _await_hard_deadline(
                    self.mcp_manager.connect_ephemeral(
                        connection_id,
                        friend.lounge_url,
                        {"Authorization": f"Bearer {friend.visitor_key}"},
                    ),
                    _TOOL_TIMEOUT_SECONDS,
                )
                self._require_approved_protocol(tools)
                try:
                    state = await self._call_tool(
                        connection_id, "get_visit_state", {}
                    )
                    recovered = self._completed_remote_response(
                        state, request_id
                    )
                    if recovered is not None:
                        return recovered, True
                except BaseException:
                    pass
                return (
                    await self._call_tool(
                        connection_id, "talk_to_host", arguments
                    ),
                    True,
                )
            except _VisitStopped:
                raise
            except Exception:
                raise _VisitStopped(
                    "interrupted", "network_reconnect_failed"
                )
        except Exception:
            raise _VisitStopped("interrupted", "network_reconnect_failed")

    async def _connect_with_one_retry(
        self, connection_id: str, friend: LoungeFriend
    ) -> list[dict]:
        for attempt in range(2):
            try:
                return await _await_hard_deadline(
                    self.mcp_manager.connect_ephemeral(
                        connection_id,
                        friend.lounge_url,
                        {"Authorization": f"Bearer {friend.visitor_key}"},
                    ),
                    _TOOL_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == 1:
                    raise _VisitStopped(
                        "interrupted", "network_reconnect_failed"
                    )
                await self._disconnect_bounded(connection_id)
        raise _VisitStopped("interrupted", "network_reconnect_failed")

    async def _end_with_one_reconnect(
        self,
        connection_id: str,
        friend: LoungeFriend,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        try:
            return await self._call_tool(connection_id, "end_visit", arguments)
        except _VisitStopped:
            raise
        except (MCPToolTransportError, TimeoutError):
            try:
                await self._disconnect_bounded(connection_id)
                tools = await _await_hard_deadline(
                    self.mcp_manager.connect_ephemeral(
                        connection_id,
                        friend.lounge_url,
                        {"Authorization": f"Bearer {friend.visitor_key}"},
                    ),
                    _TOOL_TIMEOUT_SECONDS,
                )
                self._require_approved_protocol(tools)
                return await self._call_tool(
                    connection_id, "end_visit", arguments
                )
            except _VisitStopped:
                raise
            except Exception:
                raise _VisitStopped(
                    "interrupted", "network_reconnect_failed"
                )

    async def _call_tool(
        self, connection_id: str, tool_name: str, arguments: dict
    ) -> dict[str, object]:
        if tool_name not in _APPROVED_TOOLS:
            raise _VisitStopped("interrupted", "remote_protocol_error")
        try:
            result = await _await_hard_deadline(
                self.mcp_manager.call_tool_json(
                    connection_id, tool_name, arguments
                ),
                _TOOL_TIMEOUT_SECONDS,
            )
        except MCPToolProtocolError:
            raise _VisitStopped("interrupted", "remote_protocol_error")
        if not isinstance(result, dict):
            raise _VisitStopped("interrupted", "remote_protocol_error")
        return result

    async def _disconnect_bounded(self, connection_id: str) -> None:
        try:
            await _await_hard_deadline(
                self.mcp_manager.disconnect(connection_id),
                _DISCONNECT_TIMEOUT_SECONDS,
            )
        except BaseException:
            pass

    @staticmethod
    def _completed_remote_response(
        state: dict[str, object], request_id: str
    ) -> dict[str, object] | None:
        job = state.get("job")
        if not isinstance(job, dict):
            return None
        if job.get("request_id") != request_id or job.get("status") != "completed":
            return None
        reply = job.get("visible_text")
        action = job.get("action", "continue")
        if not isinstance(reply, str) or action not in {"continue", "closing", "end"}:
            return None
        host_message_id = ""
        messages = state.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if not isinstance(message, dict) or message.get("sender") != "host":
                    continue
                candidate = message.get("id")
                if isinstance(candidate, str):
                    host_message_id = candidate
                break
        return {
            "status": "ok",
            "reply": reply,
            "action": action,
            "host_message_id": host_message_id,
        }

    @staticmethod
    def _require_approved_protocol(tools: object) -> None:
        if not isinstance(tools, list):
            raise _VisitStopped("interrupted", "unsupported_server")
        names = {
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        if not _APPROVED_TOOLS.issubset(names):
            raise _VisitStopped("interrupted", "unsupported_server")

    @staticmethod
    def _require_status(
        response: dict[str, object], accepted: set[str]
    ) -> None:
        remote_status = response.get("status")
        if remote_status in accepted:
            return
        if remote_status in _REJECTED_REMOTE_STATUSES:
            raise _VisitStopped(
                "rejected", LoungeVisitCoordinator._remote_reason(response)
            )
        if remote_status in _INTERRUPTED_REMOTE_STATUSES:
            raise _VisitStopped(
                "interrupted", LoungeVisitCoordinator._remote_reason(response)
            )
        raise _VisitStopped("interrupted", "remote_protocol_error")

    @staticmethod
    def _remote_reason(response: dict[str, object]) -> str:
        reason = response.get("reason")
        if isinstance(reason, str) and reason in _TERMINAL_REASON_CODES:
            return reason
        status = response.get("status")
        if status == "generation_failed":
            return "generation_failed_after_retries"
        if status in _REJECTED_REMOTE_STATUSES | _INTERRUPTED_REMOTE_STATUSES:
            return str(status)
        if isinstance(status, str) and status in _TERMINAL_REASON_CODES:
            return status
        return "remote_protocol_error"

    @staticmethod
    def _remote_terminal_reason(reason: str) -> str:
        return reason if reason in _TERMINAL_REASON_CODES else "unexpected_failure"

    @staticmethod
    def _validate_outbound_message(message: object) -> None:
        if not isinstance(message, str) or not message:
            raise _VisitStopped("rejected", "invalid_message")
        if len(message) > _MAX_MESSAGE_CHARS:
            raise _VisitStopped("rejected", "response_too_long")

    @staticmethod
    def _parse_composed_message(message: object) -> tuple[object, str]:
        if not isinstance(message, str):
            return message, "continue"
        match = _LOCAL_ACTION_RE.search(message)
        if match is None:
            return message.strip(), "continue"
        visible = message[: match.start()].strip()
        return visible, match.group(1)

    @classmethod
    def _pure_text_timeline(
        cls, messages: object, friend: LoungeFriend
    ) -> list[dict]:
        if not isinstance(messages, list):
            return []
        safe_messages = []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(
                message.get("content"), str
            ):
                continue
            safe = {
                field: value
                for field in _SAFE_TIMELINE_FIELDS
                if (value := message.get(field)) is not None
                and isinstance(value, str)
            }
            safe = {
                field: cls._safe_prompt_text(
                    cls._safe_remote_text(value, friend)
                )
                for field, value in safe.items()
            }
            safe_messages.append(safe)
        return safe_messages[-_MAX_TIMELINE_ENTRIES:]

    @classmethod
    def _extend_timeline(
        cls, timeline: list[dict], outbound: str, inbound: str
    ) -> list[dict]:
        return (
            timeline
            + [
                {"sender": "visitor", "content": cls._safe_prompt_text(outbound)},
                {"sender": "host", "content": cls._safe_prompt_text(inbound)},
            ]
        )[-_MAX_TIMELINE_ENTRIES:]

    @classmethod
    def _prompt_safe_friend(cls, friend: LoungeFriend) -> LoungeFriend:
        return replace(
            friend,
            lounge_url="",
            visitor_key="",
            display_name=cls._safe_prompt_text(
                redact_visitor_key(friend.display_name, friend.visitor_key)
            ),
            relationship_note=cls._safe_prompt_text(
                redact_visitor_key(friend.relationship_note, friend.visitor_key)
            ),
        )

    @staticmethod
    def _safe_prompt_text(value: str) -> str:
        value = _URL_RE.sub("[redacted]", value)
        value = _BEARER_RE.sub("[redacted]", value)
        return _SECRET_RE.sub("[redacted]", value)

    @staticmethod
    def _safe_result_text(value: str) -> str:
        value = _BEARER_RE.sub("[redacted]", value)
        return _SECRET_RE.sub("[redacted]", value)

    @classmethod
    def _safe_remote_text(cls, value: str, friend: LoungeFriend) -> str:
        for secret in (friend.visitor_key, friend.lounge_url):
            if secret:
                value = value.replace(secret, "[redacted]")
        return cls._safe_result_text(value)

    async def _finish_without_details(
        self,
        visit_id: str,
        status: Literal["completed", "interrupted", "rejected"],
        turn_count: int,
        reason: str,
        final_reply: str = "",
    ) -> LoungeVisitResult:
        if visit_id:
            try:
                await self.repository.finish(
                    visit_id,
                    status,
                    turn_count,
                    error="" if status == "completed" else reason,
                )
            except Exception:
                status = "interrupted"
                reason = "repository_failed"
        return LoungeVisitResult(visit_id, status, turn_count, final_reply, reason)
