"""
摄像头 API + 监控日志 API
"""

import time, asyncio, urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

from config import (
    SCREENSHOTS_DIR,
    MONITOR_LOGS_DIR,
    normalize_camera_wake_mode,
    save_cam_config,
)
from camera import cam, detect_cameras, read_monitor_logs
from phone_camera import phone_camera

router = APIRouter()

# ── 摄像头控制 ────────────────────────────────────
class CamConfigUpdate(BaseModel):
    camera_index: Optional[int] = None
    auto_interval_min: Optional[int] = None
    auto_interval_max: Optional[int] = None
    max_screenshots: Optional[int] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    esp32_cam_url: Optional[str] = None
    wake_mode: Optional[str] = None

@router.get("/api/cam/cameras")
async def list_cameras():
    # 跳过当前正在使用的摄像头，避免 DirectShow 设备冲突导致采集线程中断
    skip = cam.cfg["camera_index"] if cam.running else -1
    cams = await asyncio.get_event_loop().run_in_executor(None, lambda: detect_cameras(skip_index=skip))
    current = cam.cfg["camera_index"]
    cams = sorted(set(cams))
    return {"cameras": cams, "current": current}

@router.get("/api/cam/status")
async def cam_status():
    remaining = 0
    if cam.monitoring and cam._next_capture_at > 0:
        remaining = max(0, cam._next_capture_at - time.time())
    phone_status = phone_camera.status()
    try:
        from chatroom import get_chatroom_names
        _, ai_name, connor_name = get_chatroom_names()
    except Exception:
        ai_name, connor_name = "AI", "第二AI"
    return {
        "camera_open": cam.running,
        "monitoring": cam.monitoring,
        "camera_index": cam.cfg["camera_index"],
        "active_source": cam.cfg.get("active_source", "local"),
        "esp32_cam_url": cam.cfg.get("esp32_cam_url", ""),
        "esp32_bridge_active": cam._esp32_bridge_active,
        "phone_camera": phone_status,
        "auto_interval_min": cam.cfg.get("auto_interval_min", 10),
        "auto_interval_max": cam.cfg.get("auto_interval_max", 20),
        "max_screenshots": cam.cfg["max_screenshots"],
        "quiet_hours_enabled": cam.cfg.get("quiet_hours_enabled", False),
        "quiet_hours_start": cam.cfg.get("quiet_hours_start", "00:00"),
        "quiet_hours_end": cam.cfg.get("quiet_hours_end", "09:00"),
        "is_quiet_hours": cam._is_quiet_hours(),
        "next_capture_in": round(remaining),
        "wake_mode": normalize_camera_wake_mode(cam.cfg.get("wake_mode")),
        "ai_name": ai_name,
        "connor_name": connor_name,
    }

@router.post("/api/cam/open")
async def cam_open(camera_index: int = 0):
    if cam._cam_op_lock.locked():
        return {"ok": False, "message": "摄像头操作进行中，请稍候"}
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: cam.open_camera(camera_index))
    return {"ok": ok, "camera_index": cam.cfg["camera_index"],
            "message": "摄像头已打开" if ok else "无法打开摄像头，请检查连接"}

@router.post("/api/cam/close")
async def cam_close():
    loop = asyncio.get_event_loop()
    if cam.cfg.get("active_source") == "esp32":
        await loop.run_in_executor(None, cam.close_esp32)
    else:
        if cam._cam_op_lock.locked():
            return {"ok": False, "message": "摄像头操作进行中，请稍候"}
        await loop.run_in_executor(None, cam.close_camera)
    return {"ok": True}

@router.post("/api/cam/monitor/start")
async def cam_monitor_start():
    # start_monitoring 可能调用 open_camera（阻塞），需要在线程中执行
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, cam.start_monitoring)
    return {"ok": True, "monitoring": True}

@router.post("/api/cam/monitor/stop")
async def cam_monitor_stop():
    cam.stop_monitoring()
    return {"ok": True, "monitoring": False}

@router.post("/api/cam/screenshot")
async def cam_screenshot():
    filename = cam.save_screenshot()
    if not filename:
        return {"error": "无画面"}
    return {"ok": True, "filename": filename}

@router.put("/api/cam/config")
async def update_cam_config(body: CamConfigUpdate):
    if body.camera_index is not None:
        cam.cfg["camera_index"] = body.camera_index
    if body.auto_interval_min is not None:
        cam.cfg["auto_interval_min"] = max(1, body.auto_interval_min)
    if body.auto_interval_max is not None:
        cam.cfg["auto_interval_max"] = max(cam.cfg.get("auto_interval_min", 1), body.auto_interval_max)
    if body.max_screenshots is not None:
        cam.cfg["max_screenshots"] = max(0, body.max_screenshots)
    if body.quiet_hours_enabled is not None:
        cam.cfg["quiet_hours_enabled"] = body.quiet_hours_enabled
    if body.quiet_hours_start is not None:
        cam.cfg["quiet_hours_start"] = body.quiet_hours_start
    if body.quiet_hours_end is not None:
        cam.cfg["quiet_hours_end"] = body.quiet_hours_end
    if body.esp32_cam_url is not None:
        cam.cfg["esp32_cam_url"] = body.esp32_cam_url.strip()
    if body.wake_mode is not None:
        cam.cfg["wake_mode"] = normalize_camera_wake_mode(body.wake_mode)
    save_cam_config(cam.cfg)
    return {"ok": True}

@router.get("/api/cam/frame")
async def cam_frame():
    jpg = cam.get_frame_jpeg()
    if not jpg:
        return Response(content=b'', status_code=204)
    return Response(content=jpg, media_type="image/jpeg")

# ── 监控日志 ──────────────────────────────────────

# ── 画面裁剪（缩放/平移）──────────────────────────
class CropUpdate(BaseModel):
    zoom: float = 1.0
    cx: float = 0.5
    cy: float = 0.5

@router.get("/api/cam/crop")
async def get_crop():
    return cam.get_crop()

@router.put("/api/cam/crop")
async def set_crop(body: CropUpdate):
    cam.set_crop(body.zoom, body.cx, body.cy)
    return cam.get_crop()

# ── 监控日志 ──────────────────────────────────────
@router.get("/api/cam/logs")
async def list_log_dates():
    dates = []
    for f in sorted(MONITOR_LOGS_DIR.glob("*.jsonl"), reverse=True):
        dates.append(f.stem)
    return {"dates": dates}

@router.get("/api/cam/logs/{date_str}")
async def get_log_entries(date_str: str):
    entries = read_monitor_logs(date_str)
    return {"date": date_str, "entries": entries}

@router.get("/api/cam/logs/today/entries")
async def get_today_logs():
    entries = read_monitor_logs()
    return {"date": time.strftime('%Y-%m-%d'), "entries": entries}

# ── ESP32-CAM 画面源切换 ─────────────────────────
class SourceSwitch(BaseModel):
    source: str          # "local" | "esp32" | "phone"
    esp32_url: Optional[str] = None

@router.post("/api/cam/source")
async def switch_cam_source(body: SourceSwitch):
    if body.source not in ("local", "esp32", "phone"):
        return {"ok": False, "message": "source 只能是 local、esp32 或 phone"}
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: cam.switch_source(body.source, body.esp32_url))
    return {
        "ok": ok,
        "active_source": cam.cfg.get("active_source", "local"),
        "message": "切换成功" if ok else "切换失败，请检查连接",
    }

@router.get("/api/cam/esp32/ping")
async def esp32_ping():
    url = cam.cfg.get("esp32_cam_url", "").strip()
    if not url:
        return {"online": False, "message": "ESP32-CAM URL 未配置"}
    try:
        snapshot_url = url.rstrip("/") + "/capture"
        req = urllib.request.Request(snapshot_url, method="GET")
        loop = asyncio.get_event_loop()
        def _ping():
            with urllib.request.urlopen(req, timeout=5) as resp:
                return len(resp.read()) > 100
        ok = await loop.run_in_executor(None, _ping)
        return {"online": ok, "url": url}
    except Exception as e:
        return {"online": False, "message": str(e)}

# ── ESP32-CAM App 桥接帧接收 ─────────────────────
@router.post("/api/cam/esp32/frame")
async def receive_esp32_frame(request: Request):
    """接收 App 桥接推来的 JPEG 帧"""
    jpg_bytes = await request.body()
    if not jpg_bytes or len(jpg_bytes) < 100:
        return {"ok": False, "message": "无效数据"}
    ok = cam.receive_esp32_frame(jpg_bytes)
    return {"ok": ok}
