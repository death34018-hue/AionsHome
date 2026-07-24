"""
手机屏幕截图缓存：Android App 通过 MediaProjection 上传最近一帧，
监控截图合成时按需取最近可用截图。
"""

import asyncio
import base64
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from config import DATA_DIR, UPLOADS_DIR


PHONE_SCREEN_DIR = DATA_DIR / "phone_screens"
PHONE_SCREEN_DIR.mkdir(parents=True, exist_ok=True)
PHONE_SCREEN_META = PHONE_SCREEN_DIR / "latest.json"
LATEST_FILENAME = "phone_screen_latest.jpg"


def _safe_ts(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(ts or time.time()))


def save_phone_screen_b64(
    image_base64: str,
    *,
    timestamp: float | None = None,
    app: str = "",
    locked: bool = False,
    source: str = "",
    reason: str = "",
) -> dict:
    """保存 Android 上传的手机屏幕截图，只保留最新一张。"""
    raw = image_base64.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    data = base64.b64decode(raw)

    received_at = time.time()
    ts = timestamp or received_at
    fname = LATEST_FILENAME
    path = PHONE_SCREEN_DIR / fname
    path.write_bytes(data)

    upload_path = UPLOADS_DIR / fname
    upload_path.write_bytes(data)

    meta = {
        "timestamp": ts,
        "received_at": received_at,
        "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        "filename": fname,
        "path": str(path),
        "upload_path": str(upload_path),
        "url": f"/uploads/{fname}",
        "app": app,
        "locked": bool(locked),
        "source": source,
        "reason": reason,
    }
    PHONE_SCREEN_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    cleanup_old_phone_screens()
    return meta


def record_phone_screen_skip(reason: str, *, app: str = "", locked: bool = False) -> dict:
    """记录最近一次没有上传截图的原因，便于诊断。"""
    received_at = time.time()
    meta = {
        "timestamp": received_at,
        "received_at": received_at,
        "time": time.strftime("%H:%M:%S"),
        "filename": "",
        "path": "",
        "upload_path": "",
        "url": "",
        "app": app,
        "locked": bool(locked),
        "skip_reason": reason,
    }
    PHONE_SCREEN_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _read_phone_screen_meta() -> dict:
    if not PHONE_SCREEN_META.exists():
        return {}
    try:
        value = json.loads(PHONE_SCREEN_META.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def get_phone_screen_result_after(received_after: float) -> tuple[str, Path | None]:
    """Return the first server-received phone result newer than a request.

    Status is ``pending`` while no new result exists, ``ready`` for an image,
    or ``skipped`` when Android explicitly reports that capture was unavailable.
    Server receipt time is used instead of the phone clock so WAN clock skew
    cannot make a fresh upload look stale.
    """
    meta = _read_phone_screen_meta()
    try:
        received_at = float(meta.get("received_at") or 0)
    except (TypeError, ValueError):
        received_at = 0.0
    if received_at < float(received_after):
        return "pending", None
    if meta.get("skip_reason"):
        return "skipped", None
    if not meta.get("filename"):
        return "skipped", None
    path = Path(meta.get("path") or "")
    if path.exists():
        return "ready", path
    return "skipped", None


async def wait_for_phone_screen_after(
    received_after: float,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
) -> Path | None:
    """Wait without blocking the event loop for a new upload or skip report."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        status, path = get_phone_screen_result_after(received_after)
        if status == "ready":
            return path
        if status == "skipped":
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(max(0.001, float(poll_seconds)), remaining))


def wait_for_phone_screen_after_sync(
    received_after: float,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
    should_continue: Callable[[], bool] | None = None,
) -> Path | None:
    """Thread-friendly equivalent used by the sentinel patrol loop."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if should_continue is not None and not should_continue():
            return None
        status, path = get_phone_screen_result_after(received_after)
        if status == "ready":
            return path
        if status == "skipped":
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(max(0.001, float(poll_seconds)), remaining))


def freeze_phone_screen(path: Path, *, event_id: str) -> str:
    """Copy the mutable latest image to an event-specific model attachment."""
    safe_event = re.sub(r"[^A-Za-z0-9_-]+", "_", str(event_id or "checkpoint"))
    safe_event = safe_event.strip("_")[:72] or "checkpoint"
    filename = f"app_supervision_{safe_event}_{time.time_ns()}.jpg"
    destination = UPLOADS_DIR / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)

    frozen = sorted(
        UPLOADS_DIR.glob("app_supervision_*.jpg"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for expired in frozen[64:]:
        expired.unlink(missing_ok=True)
    return f"/uploads/{filename}"


def get_recent_phone_screen_path(
    max_age_seconds: int = 15,
    *,
    received_after: float | None = None,
) -> Path | None:
    """返回最近 max_age_seconds 秒内上传的手机截图路径。"""
    try:
        meta = _read_phone_screen_meta()
        if not meta.get("filename"):
            return None
        if received_after is not None:
            received_at = float(meta.get("received_at") or 0)
            if received_at < float(received_after):
                return None
        if time.time() - float(meta.get("timestamp", 0)) > max_age_seconds:
            return None
        path = Path(meta.get("path") or "")
        if path.exists():
            return path
    except Exception:
        return None
    return None


def cleanup_old_phone_screens(max_keep: int = 1):
    files = sorted(PHONE_SCREEN_DIR.glob("phone_screen_*.jpg"))
    for f in files:
        if f.name == LATEST_FILENAME:
            continue
        f.unlink(missing_ok=True)
        (UPLOADS_DIR / f.name).unlink(missing_ok=True)
