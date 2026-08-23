"""Model command handling for user-requested lounge friend visits."""

from __future__ import annotations

import re
from typing import Awaitable, Callable


# LoungeFriendStore historically uses UUID hex values; accept those as well as
# hyphenated UUIDs while keeping the protocol restricted to UUID-shaped IDs.
LOUNGE_VISIT_PATTERN = re.compile(
    r"\[LOUNGE_VISIT:([0-9a-f-]{32,36})\|([^\]]{1,200})\]",
    re.IGNORECASE,
)
_LOUNGE_VISIT_TOKEN_PATTERN = re.compile(r"\[LOUNGE_VISIT:[^\]]*\]", re.IGNORECASE)
_LOUNGE_VISIT_TRAILING_PATTERN = re.compile(r"\[LOUNGE_VISIT:[^\]]*$", re.IGNORECASE)
_LOUNGE_VISIT_PREFIX = "[LOUNGE_VISIT:"
_IMMEDIATE_VISIT_MARKER = re.compile(r"(?:现在|马上|立刻|这就|出发)")
_VISIT_ACTION = re.compile(r"(?:拜访|串门|去找.{0,20}聊)")


def is_immediate_lounge_visit_request(text: str) -> bool:
    """Return whether the user's current turn clearly requests departure now."""
    normalized = re.sub(r"\s+", "", str(text or ""))
    return bool(
        _IMMEDIATE_VISIT_MARKER.search(normalized)
        and _VISIT_ACTION.search(normalized)
    )


def is_chat_visit_friend_allowed(friend: object) -> bool:
    """Enforce the chat-visible lounge permission again at execution time."""
    return bool(
        getattr(friend, "enabled", False)
        and getattr(friend, "allow_autonomous", False)
    )


async def handle_lounge_visit_commands(
    text: str,
    *,
    actor_id: str,
    user_text: str,
    start_visit: Callable[[str, str, str], Awaitable[str]],
) -> tuple[str, list[str]]:
    """Start valid, explicitly emitted visit commands and hide every protocol tag."""
    source = text or ""
    started: list[str] = []
    matches = LOUNGE_VISIT_PATTERN.finditer(source)
    if not is_immediate_lounge_visit_request(user_text):
        matches = ()
    for match in matches:
        friend_id = match.group(1).lower()
        topic = match.group(2).strip()
        if topic:
            try:
                visit_id = await start_visit(actor_id, friend_id, topic)
            except KeyError:
                continue
            if visit_id:
                started.append(visit_id)
    visible = _LOUNGE_VISIT_TOKEN_PATTERN.sub("", source)
    visible = _LOUNGE_VISIT_TRAILING_PATTERN.sub("", visible)
    return visible.strip(), started


class LoungeVisitCommandStreamFilter:
    """Keep visit protocol tags out of streamed text and TTS, even across chunks."""

    def __init__(self) -> None:
        self._pending = ""
        self._in_command = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        buf = self._pending + chunk
        self._pending = ""
        out: list[str] = []

        while buf:
            if self._in_command:
                end = buf.find("]")
                if end < 0:
                    return "".join(out)
                buf = buf[end + 1 :]
                self._in_command = False
                continue

            start = buf.upper().find(_LOUNGE_VISIT_PREFIX)
            if start >= 0:
                out.append(buf[:start])
                buf = buf[start + len(_LOUNGE_VISIT_PREFIX) :]
                self._in_command = True
                continue

            keep = self._possible_prefix_len(buf)
            if keep:
                out.append(buf[:-keep])
                self._pending = buf[-keep:]
            else:
                out.append(buf)
            break

        return "".join(out)

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        if self._in_command:
            self._in_command = False
            return ""
        return pending

    @staticmethod
    def _possible_prefix_len(text: str) -> int:
        max_len = min(len(text), len(_LOUNGE_VISIT_PREFIX) - 1)
        upper = text.upper()
        for size in range(max_len, 0, -1):
            if _LOUNGE_VISIT_PREFIX.startswith(upper[-size:]):
                return size
        return 0
