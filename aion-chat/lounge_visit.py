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
        "visitor_busy",
        "service_busy",
        "request_conflict",
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
        progress = {"visit_id": "", "turn_count": 0, "final_reply": ""}
        async with _OUTBOUND_VISIT_LOCK:
            try:
                async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                    return await self._run_locked(
                        actor_id,
                        friend_id,
                        trigger_source,
                        topic,
                        compose_next,
                        progress,
                    )
            except TimeoutError:
                return await self._finish_without_details(
                    str(progress["visit_id"]),
                    "interrupted",
                    int(progress["turn_count"]),
                    "visit_timeout",
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

        try:
            tools = await self.mcp_manager.connect_ephemeral(
                connection_id,
                friend.lounge_url,
                {"Authorization": f"Bearer {friend.visitor_key}"},
            )
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
                    raise _VisitStopped("interrupted", "generation_failed")
                message, local_action = self._parse_composed_message(composed)
                self._validate_outbound_message(message)

                request_id = str(uuid.uuid4())
                try:
                    await self.repository.append_message(
                        visit_id, "outbound", message
                    )
                except Exception:
                    raise _VisitStopped("interrupted", "repository_failed")

                turn_count = turn
                progress["turn_count"] = turn_count
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
                        raise _VisitStopped("interrupted", "generation_failed")
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
                    ended = await self._call_tool(
                        connection_id,
                        "end_visit",
                        {"final_message": final_message},
                    )
                    self._require_status(ended, {"ok"})
                    reason = "action_end"
                    status = "completed"
                    break
            else:
                reason = "max_turns"

            if not end_attempted:
                end_attempted = True
                ended = await self._call_tool(connection_id, "end_visit", {})
                self._require_status(ended, {"ok"})
                status = "completed"
        except _VisitStopped as stopped:
            status = stopped.status
            reason = stopped.reason
        except Exception:
            status = "interrupted"
            reason = "connection_failed"
        finally:
            if visit_begun and not end_attempted:
                end_attempted = True
                try:
                    await self._call_tool(connection_id, "end_visit", {})
                except Exception:
                    pass
            try:
                await self.mcp_manager.disconnect(connection_id)
            except Exception:
                pass

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
                raise _VisitStopped("interrupted", "connection_failed")
            try:
                await self.mcp_manager.disconnect(connection_id)
            except Exception:
                pass
            try:
                tools = await self.mcp_manager.connect_ephemeral(
                    connection_id,
                    friend.lounge_url,
                    {"Authorization": f"Bearer {friend.visitor_key}"},
                )
                self._require_approved_protocol(tools)
                return (
                    await self._call_tool(
                        connection_id, "talk_to_host", arguments
                    ),
                    True,
                )
            except _VisitStopped:
                raise
            except Exception:
                raise _VisitStopped("interrupted", "connection_failed")
        except Exception:
            raise _VisitStopped("interrupted", "connection_failed")

    async def _call_tool(
        self, connection_id: str, tool_name: str, arguments: dict
    ) -> dict[str, object]:
        if tool_name not in _APPROVED_TOOLS:
            raise _VisitStopped("interrupted", "remote_protocol_error")
        try:
            result = await asyncio.wait_for(
                self.mcp_manager.call_tool_json(
                    connection_id, tool_name, arguments
                ),
                timeout=_TOOL_TIMEOUT_SECONDS,
            )
        except MCPToolProtocolError:
            raise _VisitStopped("interrupted", "remote_protocol_error")
        if not isinstance(result, dict):
            raise _VisitStopped("interrupted", "remote_protocol_error")
        return result

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
            raise _VisitStopped("rejected", str(remote_status))
        if remote_status in _INTERRUPTED_REMOTE_STATUSES:
            raise _VisitStopped("interrupted", str(remote_status))
        raise _VisitStopped("interrupted", "remote_protocol_error")

    @staticmethod
    def _validate_outbound_message(message: object) -> None:
        if not isinstance(message, str) or not message:
            raise _VisitStopped("rejected", "invalid_message")
        if len(message) > _MAX_MESSAGE_CHARS:
            raise _VisitStopped("rejected", "message_too_long")

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
