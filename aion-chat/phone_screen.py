"""
手机屏幕截图缓存：Android App 通过 MediaProjection 上传最近一帧，
监控截图合成时按需取最近可用截图。
"""

import base64
import json
import time
from pathlib import Path

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

    ts = timestamp or time.time()
    fname = LATEST_FILENAME
    path = PHONE_SCREEN_DIR / fname
    path.write_bytes(data)

    upload_path = UPLOADS_DIR / fname
    upload_path.write_bytes(data)

    meta = {
        "timestamp": ts,
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
    meta = {
        "timestamp": time.time(),
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


def get_recent_phone_screen_path(max_age_seconds: int = 15) -> Path | None:
    """返回最近 max_age_seconds 秒内上传的手机截图路径。"""
    if not PHONE_SCREEN_META.exists():
        return None
    try:
        meta = json.loads(PHONE_SCREEN_META.read_text(encoding="utf-8"))
        if not meta.get("filename"):
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
