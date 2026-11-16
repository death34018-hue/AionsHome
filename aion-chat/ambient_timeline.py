"""Format the listening device's rolling five-minute sound timeline."""
import math
import time
from datetime import datetime, timedelta, timezone

LABELS = {"speech": "说话", "cough": "咳嗽", "humming": "哼歌", "singing": "唱歌", "snoring": "鼾声"}


def format_timeline(events: list[dict], *, now_ms=None, utc_offset_minutes=480) -> str:
    now = time.time() * 1000 if now_ms is None else now_ms
    tz = timezone(timedelta(minutes=max(-720, min(840, utc_offset_minutes))))
    rows = []
    for event in events[-300:]:
        kind = event.get("kind")
        if kind not in LABELS:
            continue
        try:
            start, end = float(event["start"]), float(event["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        if end <= now - 300000 or start > now + 5000:
            continue
        start, end = max(start, now - 300000), min(end, now)
        stamp = datetime.fromtimestamp(start / 1000, tz).strftime("%H:%M:%S")
        if end - start >= 2000:
            stamp += "–" + datetime.fromtimestamp(end / 1000, tz).strftime("%H:%M:%S")
        description = LABELS[kind]
        if kind == "speech":
            text = " ".join(str(event.get("text") or "").split())[:1000]
            if not text:
                continue
            description = f"说：{text}"
        rows.append((start, f"{stamp} {description}"))
    return "\n".join(row for _, row in sorted(rows))
