"""Shared policy for preserving replies when an AI text stream is interrupted."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamFailureResolution:
    visible_text: str
    tts_text: str
    diagnostic_error: str
    had_partial_text: bool


def resolve_stream_failure(
    collected_text: str,
    error: BaseException,
    failure_label: str,
) -> StreamFailureResolution:
    diagnostic_error = str(error)
    if collected_text.strip():
        return StreamFailureResolution(
            visible_text=collected_text,
            tts_text=collected_text,
            diagnostic_error=diagnostic_error,
            had_partial_text=True,
        )
    return StreamFailureResolution(
        visible_text=f"[{failure_label}] 暂时无法完成回复，请手动重试。",
        tts_text="",
        diagnostic_error=diagnostic_error,
        had_partial_text=False,
    )
