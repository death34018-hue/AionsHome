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


async def handle_lounge_visit_commands(
    text: str,
    *,
    actor_id: str,
    start_visit: Callable[[str, str, str], Awaitable[str]],
) -> tuple[str, list[str]]:
    """Start valid, explicitly emitted visit commands and hide every protocol tag."""
    source = text or ""
    started: list[str] = []
    for match in LOUNGE_VISIT_PATTERN.finditer(source):
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
