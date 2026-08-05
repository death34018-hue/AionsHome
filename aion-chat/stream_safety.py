"""Bounded, conservative validation for streamed model text."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import zlib
from dataclasses import dataclass
from typing import AsyncIterable, Awaitable, Callable


@dataclass(frozen=True)
class StreamSafetyPolicy:
    max_chars: int
    total_timeout: float
    idle_timeout: float = 60.0
    quarantine_chars: int = 600


@dataclass(frozen=True)
class StreamSafetyResult:
    committed_text: str
    stop_reason: str | None = None
    notice: str = ""
    diagnostic_error: str | None = None


class StreamActivity(str):
    """Upstream progress that refreshes idle time without exposing text."""

    def __new__(cls):
        return super().__new__(cls, "")


CHAT_STREAM_POLICY = StreamSafetyPolicy(
    max_chars=6_000,
    total_timeout=900.0,
    idle_timeout=300.0,
    quarantine_chars=120,
)
THEATER_STREAM_POLICY = StreamSafetyPolicy(
    max_chars=24_000,
    total_timeout=900.0,
    idle_timeout=600.0,
    quarantine_chars=120,
)


_STOP_NOTICES = {
    "quality": "回复内容异常，已自动停止生成。",
    "length": "回复内容过长，已自动停止生成。",
    "idle_timeout": "回复等待超时，已自动停止生成。",
    "total_timeout": "回复生成超时，已自动停止生成。",
    "transport": "回复连接中断，已自动停止生成。",
}
_DISALLOWED_CONTROL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)
_SURROGATE = re.compile("[\ud800-\udfff]")
_REPLACEMENT_RUN = re.compile("�{8,}")
_ESCAPED_BINARY_RUN = re.compile(
    r"(?:(?:\\x[0-9a-fA-F]{2})|(?:\\u00[0-9a-fA-F]{2})){12,}"
)
_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{320,}={0,2}(?![A-Za-z0-9+/])")
_HEX_TOKEN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{480,}(?![0-9a-fA-F])")
_PROTOCOL_LINE = re.compile(r"(?im)^\s*(?:data|event|id)\s*:")
_PROTOCOL_FIELDS = re.compile(
    r'(?i)"(?:choices|delta|finish_reason|usage|object|created|model)"\s*:'
)


def _looks_corrupt(text: str) -> bool:
    if not text:
        return False
    if (
        _DISALLOWED_CONTROL.search(text)
        or _SURROGATE.search(text)
        or _REPLACEMENT_RUN.search(text)
        or _ESCAPED_BINARY_RUN.search(text)
    ):
        return True

    for match in _BASE64_TOKEN.finditer(text):
        token = match.group(0).rstrip("=")
        if len(set(token)) >= 16:
            return True
    for match in _HEX_TOKEN.finditer(text):
        if len(set(match.group(0))) >= 8:
            return True

    protocol_lines = len(_PROTOCOL_LINE.findall(text))
    protocol_fields = len(_PROTOCOL_FIELDS.findall(text))
    if protocol_lines >= 3 and protocol_fields >= 4:
        return True

    if len(text) < 1_200:
        return False

    signals = 0
    encoded = text.encode("utf-8", errors="ignore")
    if encoded and len(zlib.compress(encoded, level=1)) / len(encoded) < 0.09:
        signals += 1

    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) >= 12:
        unique_ratio = len(set(nonempty_lines)) / len(nonempty_lines)
        if unique_ratio < 0.18:
            signals += 1

    unusual = sum(
        1
        for char in text
        if not (
            char.isalnum()
            or char.isspace()
            or char.isalpha()
            or char in "，。！？；：、,.!?;:'\"“”‘’()[]{}<>《》—…_-+*/=|`~@#$%^&\\"
            or ord(char) >= 0x1F000  # emoji and pictographs
        )
    )
    if unusual / len(text) > 0.55:
        signals += 1

    return signals >= 2


class StreamSafetyGuard:
    """Hold a small suffix until it is safe to expose to UI and TTS."""

    def __init__(self, policy: StreamSafetyPolicy):
        self.policy = policy
        self._buffer = ""
        self._committed_chars = 0
        self._stop_reason: str | None = None

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def _stop(self, reason: str) -> None:
        self._stop_reason = reason
        self._buffer = ""

    def feed(self, text: str) -> str:
        if self._stop_reason or not text:
            return ""

        candidate = self._buffer + text
        remaining = self.policy.max_chars - self._committed_chars
        if len(candidate) > remaining:
            allowed = candidate[:max(0, remaining)]
            if _looks_corrupt(allowed):
                self._stop("quality")
                return ""
            self._stop_reason = "length"
            self._buffer = ""
            self._committed_chars += len(allowed)
            return allowed

        if _looks_corrupt(candidate):
            self._stop("quality")
            return ""

        self._buffer = candidate
        release_chars = len(self._buffer) - self.policy.quarantine_chars
        if release_chars <= 0:
            return ""

        committed = self._buffer[:release_chars]
        self._buffer = self._buffer[release_chars:]
        self._committed_chars += len(committed)
        return committed

    def finish(self) -> StreamSafetyResult:
        if self._stop_reason:
            return StreamSafetyResult(
                committed_text="",
                stop_reason=self._stop_reason,
                notice=_STOP_NOTICES[self._stop_reason],
            )

        tail = self._buffer
        self._buffer = ""
        if _looks_corrupt(tail):
            self._stop_reason = "quality"
            return StreamSafetyResult(
                committed_text="",
                stop_reason="quality",
                notice=_STOP_NOTICES["quality"],
            )
        self._committed_chars += len(tail)
        return StreamSafetyResult(committed_text=tail)


async def _deliver(
    callback: Callable[[str], object | Awaitable[object]],
    text: str,
) -> None:
    if not text:
        return
    result = callback(text)
    if inspect.isawaitable(result):
        await result


async def _close_async_iterator(iterator, source) -> None:
    close = getattr(iterator, "aclose", None) or getattr(source, "aclose", None)
    if close is not None:
        await close()


async def consume_safe_stream(
    source: AsyncIterable[str | StreamActivity],
    policy: StreamSafetyPolicy,
    on_commit: Callable[[str], object | Awaitable[object]],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> StreamSafetyResult:
    """Consume a provider stream and expose only validated text."""

    guard = StreamSafetyGuard(policy)
    iterator = source.__aiter__()
    started_at = clock()
    committed_parts: list[str] = []
    diagnostic_error: str | None = None

    while guard.stop_reason is None:
        total_remaining = policy.total_timeout - (clock() - started_at)
        if total_remaining <= 0:
            guard._stop("total_timeout")
            break
        wait_seconds = min(policy.idle_timeout, total_remaining)
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), wait_seconds)
        except StopAsyncIteration:
            break
        except TimeoutError:
            reason = (
                "total_timeout"
                if clock() - started_at >= policy.total_timeout
                else "idle_timeout"
            )
            guard._stop(reason)
            break
        except Exception as error:
            diagnostic_error = str(error)
            guard._stop("transport")
            break

        if isinstance(chunk, StreamActivity):
            continue

        committed = guard.feed(chunk)
        if committed:
            committed_parts.append(committed)
            await _deliver(on_commit, committed)

    result = guard.finish()
    if result.committed_text:
        committed_parts.append(result.committed_text)
        await _deliver(on_commit, result.committed_text)

    if result.stop_reason:
        await _close_async_iterator(iterator, source)

    return StreamSafetyResult(
        committed_text="".join(committed_parts),
        stop_reason=result.stop_reason,
        notice=result.notice,
        diagnostic_error=diagnostic_error,
    )
