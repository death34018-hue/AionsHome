"""Select the legacy or opt-in safe-live consumer for one chat request."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from stream_safety import StreamSafetyResult
from safe_live_stream import has_provider_error_marker


@dataclass(frozen=True)
class RealtimeTransportOutcome:
    result: StreamSafetyResult
    visible_text: str
    used_fallback: bool = False
    manual_retry_required: bool = False


async def _call(callback: Callable[[], Any | Awaitable[Any]]) -> Any:
    value = callback()
    return await value if inspect.isawaitable(value) else value


def _audio_checkpoint_started(tts_streamer) -> bool:
    if tts_streamer is None:
        return False
    return bool(getattr(tts_streamer, "has_emitted_audio", False))


async def _rewind_tts_before_audio(tts_streamer) -> bool:
    if tts_streamer is None:
        return True
    rewind = getattr(tts_streamer, "rewind_before_audio", None)
    if rewind is not None:
        value = rewind()
        return bool(await value) if inspect.isawaitable(value) else bool(value)
    if getattr(tts_streamer, "accepted_segment_count", 0):
        return False
    tts_streamer.discard_pending_text()
    return True


async def consume_realtime_transport(
    *,
    mode: str,
    source_factory: Callable[[], Any],
    safe_consumer: Callable[[Any], Awaitable[tuple[StreamSafetyResult, str]]],
    legacy_consumer: Callable[[Any], Awaitable[tuple[StreamSafetyResult, str]]],
    reset_visible: Callable[[], Any | Awaitable[Any]],
    tts_streamer=None,
) -> RealtimeTransportOutcome:
    """Run one safe attempt and, while reversible, one legacy fallback."""
    if mode != "safe_live":
        result, visible = await legacy_consumer(source_factory())
        return RealtimeTransportOutcome(result=result, visible_text=visible)

    result, visible = await safe_consumer(source_factory())
    if result.stop_reason is None:
        return RealtimeTransportOutcome(result=result, visible_text=visible)

    await _call(reset_visible)
    if _audio_checkpoint_started(tts_streamer):
        if tts_streamer is not None:
            tts_streamer.cancel()
        failed = StreamSafetyResult(
            committed_text="",
            stop_reason=result.stop_reason,
            notice="回复连接异常，可重试",
            diagnostic_error=result.diagnostic_error,
        )
        return RealtimeTransportOutcome(
            result=failed,
            visible_text="",
            manual_retry_required=True,
        )

    if not await _rewind_tts_before_audio(tts_streamer):
        if tts_streamer is not None:
            tts_streamer.cancel()
        return RealtimeTransportOutcome(
            result=StreamSafetyResult(
                committed_text="",
                stop_reason=result.stop_reason,
                notice="回复连接异常，可重试",
                diagnostic_error=result.diagnostic_error,
            ),
            visible_text="",
            manual_retry_required=True,
        )
    fallback_result, fallback_visible = await legacy_consumer(source_factory())
    fallback_has_provider_error = has_provider_error_marker(fallback_result.committed_text)
    if fallback_result.stop_reason is not None or fallback_has_provider_error:
        await _call(reset_visible)
        if tts_streamer is not None:
            tts_streamer.cancel()
        return RealtimeTransportOutcome(
            result=StreamSafetyResult(
                committed_text="",
                stop_reason=fallback_result.stop_reason or "transport",
                notice=fallback_result.notice or "回复连接异常，可重试",
                diagnostic_error=fallback_result.diagnostic_error,
            ),
            visible_text="",
            used_fallback=True,
            manual_retry_required=True,
        )
    return RealtimeTransportOutcome(
        result=fallback_result,
        visible_text=fallback_visible,
        used_fallback=True,
    )
