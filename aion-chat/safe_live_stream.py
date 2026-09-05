"""Independent low-latency safety gate for explicitly enabled chat models."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass
from typing import AsyncIterable, Awaitable, Callable

from stream_safety import StreamActivity, StreamSafetyResult


SAFE_LIVE_QUARANTINE_CHARS = 16
SAFE_LIVE_MAX_PENDING_COMMAND_CHARS = 512
SAFE_LIVE_MAX_LONG_COMMAND_CHARS = 4_096
SAFE_LIVE_MAX_CHARS = 6_000
SAFE_LIVE_TOTAL_TIMEOUT = 900.0
SAFE_LIVE_IDLE_TIMEOUT = 300.0

_DISALLOWED_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_REPLACEMENT_RUN = re.compile("�{8,}")
_THINK_MARKER = re.compile(r"(?i)</?\s*think(?:ing)?\b")
_SSE_LINE = re.compile(r"(?im)^\s*(?:data|event|id)\s*:")
_RAW_OPENAI_START = re.compile(
    r'(?is)(?:^|[\r\n])\s*(?:data:\s*)?\{\s*"(?:choices|object|id|created|model)"\s*:'
)
_RAW_OPENAI_ENVELOPE = re.compile(
    r'(?is)^\s*(?:data:\s*)?\{.{0,240}"(?:choices|object)"\s*:.*'
    r'"(?:delta|finish_reason|usage)"\s*:'
)
_UNKNOWN_ANGLE_TAG = re.compile(r"(?is)<\s*/?\s*([a-z][a-z0-9_-]*)\b[^>]{0,160}>")
_ALLOWED_CONTROL_TAGS = {"meta", "autonomy_state"}
_PROVIDER_ERROR_MARKER = re.compile(
    r"\[(?:CodexCLI|Gemini|自定义中转站)错误(?:[^\]]*)\]",
    re.IGNORECASE,
)
_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{320,}={0,2}(?![A-Za-z0-9+/])")

_BRACKET_COMMAND_PREFIXES = tuple(value.upper() for value in (
    "[WEB_SEARCH", "[WEB_EXTRACT", "[LOUNGE_VISIT", "[MUSIC", "[MOMENT",
    "[MEMORY", "[许愿", "[查看动态", "[SELFIE", "[DRAW", "[SONG",
    "[POI_SEARCH", "[TOY", "[PET", "[HOME", "[BAND_VIBRATE", "[BAND_NOTE",
    "[LUCKIN", "[转账", "[悄悄话", "[WECHAT", "[ALARM", "[REMINDER",
    "[MONITOR", "[SCHEDULE_DEL", "[SCHEDULE_LIST", "[NEXT_CHAT", "[HEART",
    "[视频电话", "[CAM_CHECK", "[微信消息", "[拍拍抱枕", "[剧场属性", "[剧场道具",
    "[DATE_", "[APP_", "[DEVICE_",
))
_FULLWIDTH_COMMAND_PREFIXES = ("【小组件", "【横幅")
_ANGLE_COMMAND_PREFIXES = ("<META", "<AUTONOMY_STATE")

_LONG_FORM_BRACKET_PREFIXES = (
    "[WEB_SEARCH", "[WEB_EXTRACT", "[LOUNGE_VISIT", "[MUSIC", "[MOMENT",
    "[MEMORY", "[许愿", "[SELFIE", "[DRAW", "[POI_SEARCH", "[BAND_NOTE",
    "[LUCKIN", "[悄悄话", "[WECHAT", "[ALARM", "[REMINDER", "[MONITOR",
    "[HEART", "[微信消息", "[拍拍抱枕", "[HOME", "[APP_", "[DEVICE_",
    "[DATE_",
)


def has_provider_error_marker(text: str) -> bool:
    return bool(_PROVIDER_ERROR_MARKER.search(text or ""))


def _looks_unsafe_protocol(text: str) -> bool:
    if not text:
        return False
    if (
        _DISALLOWED_CONTROL.search(text)
        or _SURROGATE.search(text)
        or _REPLACEMENT_RUN.search(text)
        or _THINK_MARKER.search(text)
        or _SSE_LINE.search(text)
        or _RAW_OPENAI_START.search(text)
        or _RAW_OPENAI_ENVELOPE.search(text)
    ):
        return True
    for match in _UNKNOWN_ANGLE_TAG.finditer(text):
        if match.group(1).lower() not in _ALLOWED_CONTROL_TAGS:
            return True
    for match in _BASE64_TOKEN.finditer(text):
        token = match.group(0).rstrip("=")
        if len(set(token)) >= 16:
            return True
    return False


class SafeLiveStreamGuard:
    """Validate text incrementally while retaining a sixteen-character tail."""

    def __init__(self):
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
        if has_provider_error_marker(candidate):
            self._stop("transport")
            return ""
        remaining = SAFE_LIVE_MAX_CHARS - self._committed_chars
        if len(candidate) > remaining:
            self._stop("length")
            return ""
        if _looks_unsafe_protocol(candidate):
            self._stop("quality")
            return ""
        self._buffer = candidate
        release_chars = len(candidate) - SAFE_LIVE_QUARANTINE_CHARS
        last_angle_open = candidate.rfind("<")
        last_angle_close = candidate.rfind(">")
        if last_angle_open > last_angle_close:
            if len(candidate) - last_angle_open > SAFE_LIVE_MAX_PENDING_COMMAND_CHARS:
                self._stop("quality")
                return ""
            release_chars = min(release_chars, last_angle_open)
        if release_chars <= 0:
            return ""
        released = candidate[:release_chars]
        self._buffer = candidate[release_chars:]
        self._committed_chars += len(released)
        return released

    def finish(self) -> StreamSafetyResult:
        if self._stop_reason:
            return StreamSafetyResult(
                committed_text="",
                stop_reason=self._stop_reason,
                notice="回复连接异常，可重试",
            )
        tail = self._buffer
        self._buffer = ""
        if _looks_unsafe_protocol(tail):
            self._stop_reason = "quality"
            return StreamSafetyResult(
                committed_text="",
                stop_reason="quality",
                notice="回复连接异常，可重试",
            )
        self._committed_chars += len(tail)
        return StreamSafetyResult(committed_text=tail)


class KnownCommandStreamFilter:
    """Hide known local control blocks while preserving them in raw text."""

    def __init__(self):
        self._pending = ""
        self._close_token = ""
        self._active_chars = 0
        self._active_tail = ""
        self._active_limit = SAFE_LIVE_MAX_PENDING_COMMAND_CHARS
        self.stop_reason: str | None = None

    @staticmethod
    def _prefixes_for(char: str) -> tuple[str, ...]:
        if char == "[":
            return _BRACKET_COMMAND_PREFIXES
        if char == "【":
            return _FULLWIDTH_COMMAND_PREFIXES
        if char == "<":
            return _ANGLE_COMMAND_PREFIXES
        return ()

    @staticmethod
    def _find_start(text: str) -> int:
        positions = [pos for token in ("[", "【", "<") if (pos := text.find(token)) >= 0]
        return min(positions) if positions else -1

    def _start_if_known_or_partial(self, candidate: str) -> tuple[bool, bool]:
        upper = candidate.upper()
        prefixes = self._prefixes_for(candidate[0])
        matched_prefix = next((prefix for prefix in prefixes if upper.startswith(prefix)), "")
        known = bool(matched_prefix)
        partial = any(prefix.startswith(upper) for prefix in prefixes)
        if known:
            if upper.startswith("[SONG"):
                self._close_token = "[/SONG]"
            elif candidate[0] == "【":
                self._close_token = "】"
            elif candidate[0] == "<":
                self._close_token = "</META>" if upper.startswith("<META") else "</AUTONOMY_STATE>"
            else:
                self._close_token = "]"
            if matched_prefix == "[SONG":
                self._active_limit = SAFE_LIVE_MAX_CHARS
            elif (
                matched_prefix in _LONG_FORM_BRACKET_PREFIXES
                or matched_prefix in _FULLWIDTH_COMMAND_PREFIXES
                or matched_prefix in _ANGLE_COMMAND_PREFIXES
            ):
                self._active_limit = SAFE_LIVE_MAX_LONG_COMMAND_CHARS
            else:
                self._active_limit = SAFE_LIVE_MAX_PENDING_COMMAND_CHARS
        return known, partial

    def feed(self, chunk: str) -> str:
        if self.stop_reason or not chunk:
            return ""
        buf = self._pending + chunk
        self._pending = ""
        out: list[str] = []

        while buf:
            if self._close_token:
                active_buf = self._active_tail + buf
                upper = active_buf.upper()
                end = upper.find(self._close_token)
                if end < 0:
                    tail_chars = min(len(active_buf), len(self._close_token) - 1)
                    consumed_chars = len(active_buf) - tail_chars
                    self._active_chars += consumed_chars
                    self._active_tail = active_buf[consumed_chars:]
                    if self._active_chars + len(self._active_tail) > self._active_limit:
                        self.stop_reason = "quality"
                    return "".join(out)
                consumed = end + len(self._close_token)
                if self._active_chars + consumed > self._active_limit:
                    self.stop_reason = "quality"
                    return "".join(out)
                buf = active_buf[consumed:]
                self._close_token = ""
                self._active_chars = 0
                self._active_tail = ""
                self._active_limit = SAFE_LIVE_MAX_PENDING_COMMAND_CHARS
                continue

            start = self._find_start(buf)
            if start < 0:
                out.append(buf)
                break
            if start > 0:
                out.append(buf[:start])
                buf = buf[start:]

            known, partial = self._start_if_known_or_partial(buf)
            if known:
                self._active_chars = 0
                continue
            if partial:
                self._pending = buf
                break
            out.append(buf[0])
            buf = buf[1:]

        return "".join(out)

    def finish(self) -> tuple[str, str | None]:
        if self.stop_reason:
            return "", self.stop_reason
        if self._close_token:
            self.stop_reason = "quality"
            return "", self.stop_reason
        pending = self._pending
        self._pending = ""
        if pending:
            _known, partial = self._start_if_known_or_partial(pending)
            if partial or self._close_token:
                self.stop_reason = "quality"
                return "", self.stop_reason
        return pending, None


async def _deliver(callback: Callable[[str], object | Awaitable[object]], text: str) -> None:
    if not text:
        return
    result = callback(text)
    if inspect.isawaitable(result):
        await result


async def _close_async_iterator(iterator, source) -> None:
    close = getattr(iterator, "aclose", None) or getattr(source, "aclose", None)
    if close is not None:
        await close()


async def consume_safe_live_stream(
    source: AsyncIterable[str | StreamActivity],
    on_commit: Callable[[str], object | Awaitable[object]],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> StreamSafetyResult:
    """Consume typed model text and expose only protocol-checked visible text."""
    guard = SafeLiveStreamGuard()
    command_filter = KnownCommandStreamFilter()
    iterator = source.__aiter__()
    started_at = clock()
    raw_parts: list[str] = []
    diagnostic_error: str | None = None

    while guard.stop_reason is None and command_filter.stop_reason is None:
        remaining = SAFE_LIVE_TOTAL_TIMEOUT - (clock() - started_at)
        if remaining <= 0:
            guard._stop("total_timeout")
            break
        try:
            chunk = await asyncio.wait_for(
                iterator.__anext__(),
                min(SAFE_LIVE_IDLE_TIMEOUT, remaining),
            )
        except StopAsyncIteration:
            break
        except TimeoutError:
            guard._stop("idle_timeout")
            break
        except Exception as error:
            diagnostic_error = str(error)
            guard._stop("transport")
            break

        if isinstance(chunk, StreamActivity):
            continue
        released = guard.feed(chunk)
        if released:
            raw_parts.append(released)
            await _deliver(on_commit, command_filter.feed(released))

    tail_result = guard.finish()
    if tail_result.committed_text and command_filter.stop_reason is None:
        raw_parts.append(tail_result.committed_text)
        await _deliver(on_commit, command_filter.feed(tail_result.committed_text))

    visible_tail = ""
    command_reason = command_filter.stop_reason
    if tail_result.stop_reason is None and command_reason is None:
        visible_tail, command_reason = command_filter.finish()
        await _deliver(on_commit, visible_tail)

    stop_reason = tail_result.stop_reason or command_reason
    if stop_reason:
        await _close_async_iterator(iterator, source)

    return StreamSafetyResult(
        committed_text="".join(raw_parts),
        stop_reason=stop_reason,
        notice="回复连接异常，可重试" if stop_reason else "",
        diagnostic_error=diagnostic_error,
    )
