"""Shared low-latency, permissive safety gate for daily chat models."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass
from typing import AsyncIterable, Awaitable, Callable

from stream_safety import StreamActivity, StreamSafetyResult
from expressive_voice_markup import BRACKETS, LEGACY_OPEN


SAFE_LIVE_QUARANTINE_CHARS = 16
SAFE_LIVE_MAX_PENDING_COMMAND_CHARS = 512
SAFE_LIVE_MAX_LONG_COMMAND_CHARS = 4_096
# Control blocks (e.g. song lyrics) retain their existing separate budget.
SAFE_LIVE_MAX_CHARS = 6_000
SAFE_LIVE_MAX_VISIBLE_CHARS = 3_000
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
    r"\[(?:CodexCLI|AntigravityCLI|Gemini|硅基流动|中转站|自定义中转站)?错误(?:[^\]]*)\]",
    re.IGNORECASE,
)
_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{320,}={0,2}(?![A-Za-z0-9+/])")

# Deliberately permissive: only long, exact loops with real vocabulary.
# Laughter, emoji, and a few repeated sentences are normal conversation.
_PARAGRAPH_LOOP = re.compile(r"(.{32,200}?)\1{5,}", re.DOTALL)
_STOP_NOTICES = {
    "length": "回复达到 3000 字符上限，已停止生成。",
    "repetition": "检测到持续重复，已停止生成。",
    "quality": "回复内容异常，已停止生成。",
    "transport": "回复连接中断，可重试。",
    "idle_timeout": "回复等待超时，可重试。",
    "total_timeout": "回复生成超时，可重试。",
}


def _is_paragraph_loop(text: str) -> bool:
    if len(text) < 800 or len(set(text)) < 12:
        return False
    return any(len(match[0]) >= 800 and len(set(match[1])) >= 12
               for match in _PARAGRAPH_LOOP.finditer(text))

_BRACKET_COMMAND_PREFIXES = tuple(value.upper() for value in (
    "[语音", "[猜拳", "[小游戏",
    "[WEB_SEARCH", "[WEB_EXTRACT", "[LOUNGE_VISIT", "[MUSIC", "[MOMENT",
    "[MEMORY", "[许愿", "[查看动态", "[SELFIE", "[DRAW", "[SONG", "[COMPOSE",
    "[POI_SEARCH", "[TOY", "[SVAKOM", "[ANKNI", "[PET", "[HOME", "[BAND_VIBRATE", "[BAND_NOTE",
    "[LUCKIN", "[转账", "[悄悄话", "[WECHAT", "[ALARM", "[REMINDER",
    "[MONITOR", "[SCHEDULE_DEL", "[SCHEDULE_LIST", "[NEXT_CHAT", "[HEART",
    "[视频电话", "[CAM_CHECK", "[微信消息", "[拍拍抱枕", "[剧场属性", "[剧场道具",
    "[DATE_", "[APP_", "[DEVICE_", "[COME_HOME", "[LOOK_AT_ME", "[PAT:", "[PAT：",
))
_FULLWIDTH_COMMAND_PREFIXES = ("【小组件", "【横幅")
_ANGLE_COMMAND_PREFIXES = ("<META", "<AUTONOMY_STATE")

_LONG_FORM_BRACKET_PREFIXES = (
    "[语音",
    "[WEB_SEARCH", "[WEB_EXTRACT", "[LOUNGE_VISIT", "[MUSIC", "[MOMENT",
    "[MEMORY", "[许愿", "[SELFIE", "[DRAW", "[POI_SEARCH", "[BAND_NOTE",
    "[LUCKIN", "[悄悄话", "[WECHAT", "[ALARM", "[REMINDER", "[MONITOR",
    "[HEART", "[微信消息", "[拍拍抱枕", "[HOME", "[APP_", "[DEVICE_",
    "[DATE_", "[COME_HOME", "[LOOK_AT_ME", "[SVAKOM", "[ANKNI",
)


def has_provider_error_marker(text: str) -> bool:
    return bool(_PROVIDER_ERROR_MARKER.search(text or ""))


def _mask_composition_angles(text: str) -> str:
    # Mini-notation uses <c4 g4>; these characters belong to a hidden score.
    return re.sub(r'\[COMPOSE\][\s\S]*?(?:\[/COMPOSE\]|$)',
                  lambda match: ' ' * len(match[0]), text, flags=re.I)


def _looks_unsafe_protocol(text: str, *, angle_text: str | None = None) -> bool:
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
    for match in _UNKNOWN_ANGLE_TAG.finditer(text if angle_text is None else angle_text):
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
        self._committed_raw = ""
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
        over_limit = len(candidate) > remaining
        candidate = candidate[:remaining]
        angle_candidate = _mask_composition_angles(self._committed_raw + candidate)[self._committed_chars:]
        if _looks_unsafe_protocol(candidate, angle_text=angle_candidate):
            self._stop("quality")
            return ""
        if over_limit:
            self._committed_chars += len(candidate)
            self._stop("length")
            return candidate
        self._buffer = candidate
        release_chars = len(candidate) - SAFE_LIVE_QUARANTINE_CHARS
        last_angle_open = angle_candidate.rfind("<")
        last_angle_close = angle_candidate.rfind(">")
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
        self._committed_raw += released
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
        angle_tail = _mask_composition_angles(self._committed_raw + tail)[self._committed_chars:]
        if _looks_unsafe_protocol(tail, angle_text=angle_tail):
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

    def __init__(self, *, preserve_private_whispers: bool = False):
        self._pending = ""
        self._close_token = ""
        self._preserve_private_whispers = preserve_private_whispers
        self._preserved_command: str | None = None
        self._any_close_bracket = False
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
        positions = [pos for token in ("[", "［", "【", "<") if (pos := text.find(token)) >= 0]
        return min(positions) if positions else -1

    def _start_if_known_or_partial(self, candidate: str) -> tuple[bool, bool]:
        # Wait for the character after 语音 to distinguish a block from the legacy
        # one-bracket command, even when the model emits one character at a time.
        voice_prefix = candidate[:4].translate(BRACKETS)
        if '[语音'.startswith(voice_prefix) or re.fullmatch(r'[\[【［]语音\s*', candidate):
            return False, True
        if voice_prefix == '[语音]':
            self._close_token = '[/语音]'
            self._active_limit = SAFE_LIVE_MAX_LONG_COMMAND_CHARS
            return True, False
        if re.match(LEGACY_OPEN, candidate):
            self._any_close_bracket = True
            self._close_token = ']'
            self._active_limit = SAFE_LIVE_MAX_LONG_COMMAND_CHARS
            return True, False
        # Match the memory parser's bracket/colon variants before the generic
        # [MEMORY prefix can claim the tag and require an ASCII closing bracket.
        if re.match(r"^[\[［【]\s*MEMORY_SEARCH\s*[:：]", candidate, re.I):
            self._any_close_bracket = True
            self._close_token = "]"
            self._active_limit = SAFE_LIVE_MAX_LONG_COMMAND_CHARS
            return True, False
        memory_prefix = re.fullmatch(r"[\[［【]\s*([A-Za-z_]*)\s*", candidate)
        if memory_prefix and "MEMORY_SEARCH".startswith(memory_prefix.group(1).upper()):
            return False, True
        upper = candidate.upper()
        prefixes = self._prefixes_for(candidate[0])
        matched_prefix = next((prefix for prefix in prefixes if upper.startswith(prefix)), "")
        known = bool(matched_prefix)
        partial = any(prefix.startswith(upper) for prefix in prefixes)
        if known:
            if self._preserve_private_whispers and matched_prefix == "[悄悄话":
                self._preserved_command = ""
            if upper.startswith("[SONG"):
                self._close_token = "[/SONG]"
            elif upper.startswith("[COMPOSE"):
                self._close_token = "[/COMPOSE]"
            elif candidate[0] == "【":
                self._close_token = "】"
            elif candidate[0] == "<":
                self._close_token = "</META>" if upper.startswith("<META") else "</AUTONOMY_STATE>"
            else:
                self._close_token = "]"
            if matched_prefix in ("[SONG", "[COMPOSE"):
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
                if self._any_close_bracket:
                    closing = re.search(r"[\]］】]", active_buf)
                    end = closing.start() if closing else -1
                elif self._close_token == '[/语音]':
                    end = active_buf.translate(BRACKETS).find(self._close_token)
                else:
                    end = upper.find(self._close_token)
                if end < 0:
                    tail_chars = min(len(active_buf), len(self._close_token) - 1)
                    consumed_chars = len(active_buf) - tail_chars
                    self._active_chars += consumed_chars
                    self._active_tail = active_buf[consumed_chars:]
                    if self._preserved_command is not None:
                        self._preserved_command += active_buf[:consumed_chars]
                    if self._active_chars + len(self._active_tail) > self._active_limit:
                        self.stop_reason = "quality"
                    return "".join(out)
                consumed = end + len(self._close_token)
                if self._active_chars + consumed > self._active_limit:
                    self.stop_reason = "quality"
                    return "".join(out)
                if self._preserved_command is not None:
                    out.append(self._preserved_command + active_buf[:consumed])
                    self._preserved_command = None
                buf = active_buf[consumed:]
                self._close_token = ""
                self._any_close_bracket = False
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
    idle_timeout: float | None = None,
    preserve_private_whispers: bool = False,
) -> StreamSafetyResult:
    """Consume typed model text and expose only protocol-checked visible text."""
    guard = SafeLiveStreamGuard()
    command_filter = KnownCommandStreamFilter(preserve_private_whispers=preserve_private_whispers)
    iterator = source.__aiter__()
    started_at = clock()
    raw_parts: list[str] = []
    visible_history = ""
    last_repeat_check = 0
    diagnostic_error: str | None = None

    async def deliver_checked(text: str) -> None:
        nonlocal visible_history, last_repeat_check
        remaining = SAFE_LIVE_MAX_VISIBLE_CHARS - len(visible_history)
        over_limit = len(text) > remaining
        text = text[:remaining]
        candidate = visible_history + text
        # Check across provider chunk boundaries without delaying ordinary text.
        if len(candidate) - last_repeat_check >= 64:
            last_repeat_check = len(candidate)
            if _is_paragraph_loop(candidate):
                guard._stop("repetition")
                return
        visible_history = candidate
        await _deliver(on_commit, text)
        if over_limit:
            guard._stop("length")

    while guard.stop_reason is None and command_filter.stop_reason is None:
        remaining = SAFE_LIVE_TOTAL_TIMEOUT - (clock() - started_at)
        if remaining <= 0:
            guard._stop("total_timeout")
            break
        try:
            chunk = await asyncio.wait_for(
                iterator.__anext__(),
                min(SAFE_LIVE_IDLE_TIMEOUT if idle_timeout is None else idle_timeout, remaining),
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
            await deliver_checked(command_filter.feed(released))

    tail_result = guard.finish()
    if tail_result.committed_text and command_filter.stop_reason is None:
        raw_parts.append(tail_result.committed_text)
        await deliver_checked(command_filter.feed(tail_result.committed_text))

    visible_tail = ""
    command_reason = command_filter.stop_reason
    if tail_result.stop_reason is None and command_reason is None:
        visible_tail, command_reason = command_filter.finish()
        await deliver_checked(visible_tail)

    stop_reason = guard.stop_reason or tail_result.stop_reason or command_reason
    if stop_reason:
        await _close_async_iterator(iterator, source)

    return StreamSafetyResult(
        # Stopped output must never execute any commands from the partial reply.
        committed_text=visible_history if stop_reason else "".join(raw_parts),
        stop_reason=stop_reason,
        notice=_STOP_NOTICES.get(stop_reason, "回复已停止，可重试。") if stop_reason else "",
        diagnostic_error=diagnostic_error,
    )
