"""
设备活动日志 API：上报、查询日期列表、查询指定日期日志、查询最近 N 小时日志
"""

import json, time

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import aiosqlite

from activity import (
    append_activity_log, read_activity_logs, read_recent_activity,
    get_available_dates, cleanup_old_activity_logs, KEEP_HOURS,
    resolve_app_name, pc_tracker, generate_activity_summary,
    is_activity_tracking_enabled, set_activity_tracking_enabled,
    pc_display_tracker,
)
from ws import manager
from database import get_db
from config import load_worldbook
from chatroom import load_chatroom_config
from location import load_location_status

router = APIRouter()

SEEKY_EVENT_PHRASES = {
    "feed": "投喂了",
    "clean": "清理了",
    "play": "玩耍了",
    "tease": "逗弄了",
    "scare": "吓唬了",
    "tap_glass": "拍打了玻璃",
    "threaten": "威胁了",
}


# ── 上报 ──────────────────────────────────────────

class ActivityReport(BaseModel):
    device: str             # "phone" | "pc"
    app: str                # 应用名 / 进程名
    title: Optional[str] = ""   # 窗口标题 / 额外描述
    timestamp: Optional[float] = None  # 客户端时间戳，不传则用服务端时间


@router.post("/api/activity/report")
async def report_activity(report: ActivityReport):
    """接收设备活动上报"""
    # 解析 App 名称（包名 → 中文名）
    resolved = resolve_app_name(report.app, report.title or "")
    if resolved is None:
        # 需要过滤的系统应用（桌面、SystemUI 等）
        return {"ok": True, "filtered": True}

    now = time.time()
    ts = report.timestamp or now

    entry = {
        "timestamp": ts,
        "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "device": report.device,
        "app": resolved,
        "title": report.title or "",
    }

    append_activity_log(entry)

    # 每次上报后顺带清理过期数据
    try:
        cleanup_old_activity_logs()
    except Exception:
        pass

    # 广播给前端
    await manager.broadcast({
        "type": "activity_log",
        "data": entry
    })

    return {"ok": True}


# ── 查询 ──────────────────────────────────────────

@router.get("/api/activity/status")
async def activity_tracker_status():
    """PC 采集线程状态诊断"""
    return {
        "pc_tracker_running": pc_tracker._running,
        "thread_alive": pc_tracker._thread is not None and pc_tracker._thread.is_alive(),
        "last_title": pc_tracker._last_title,
        "has_event_loop": pc_tracker._event_loop is not None,
        "interval": pc_tracker.interval,
        "pc_display": pc_display_tracker.get_status(),
    }

def _resolve_entries(entries: list) -> list:
    """对历史条目做名称解析 + 过滤"""
    result = []
    for e in entries:
        if e.get("device") == "home" and e.get("kind") == "home_sensor":
            continue
        resolved = resolve_app_name(e.get("app", ""), e.get("title", ""))
        if resolved is None:
            continue  # 过滤系统应用
        e["app"] = resolved
        result.append(e)
    return result


def _json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _clip(text: str, max_len: int = 180) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _timeline_names() -> dict:
    wb = load_worldbook()
    try:
        cr = load_chatroom_config()
    except Exception:
        cr = {}
    return {
        "user": wb.get("user_name", "用户"),
        "assistant": wb.get("ai_name", "AI"),
        "aion": wb.get("ai_name", "AI"),
        "connor": cr.get("connor_name", "第二位AI"),
        "system": "系统",
    }


def _actor_name(actor: str, names: dict) -> str:
    return names.get(actor, actor or "未知")


def _timeline_item(ts: float, kind: str, actor: str, title: str, detail: str = "", source_id: str = "", attachments=None) -> dict:
    return {
        "timestamp": ts,
        "time": time.strftime("%H:%M", time.localtime(ts)),
        "kind": kind,
        "actor": actor,
        "title": title,
        "detail": detail,
        "source_id": source_id,
        "attachments": attachments or [],
    }


def _idle_event_timeline_title(row, actor: str, shown_diary_ids: set[str], shown_moment_ids: set[str]) -> Optional[str]:
    action = str(row["action"] or "")
    result_type = str(row["result_type"] or "")
    result_id = str(row["result_id"] or "")
    try:
        meta = json.loads(row["metadata"] or "{}")
    except Exception:
        meta = {}

    if action == "select":
        return None
    if action == "seeky_interaction":
        phrase = SEEKY_EVENT_PHRASES.get(str(meta.get("seeky_action") or ""))
        return f"{actor}对Seeky{phrase}" if phrase else row["title"]
    if action == "home_dynamics_result":
        return None
    if action.endswith("_result") and result_type == "message":
        return None
    if action == "memory_browse_result":
        if result_type == "diary" and result_id in shown_diary_ids:
            return None
        if result_type == "moment" and result_id in shown_moment_ids:
            return None
        if result_type not in ("diary", "moment"):
            return None
    if action not in (
        "home_dynamics",
        "memory_browse",
        "memory_browse_result",
        "seeky_interaction",
        "role_chat",
        "cam_check",
        "wish_pool",
        "error",
    ):
        return None
    return row["title"]


@router.get("/api/timeline")
async def get_timeline(hours: int = 24, limit: int = 300):
    hours = max(1, min(hours, 24 * 30))
    limit = max(20, min(limit, 1000))
    cutoff = time.time() - hours * 3600
    names = _timeline_names()
    user_name = _actor_name("user", names)
    items = []
    shown_moment_ids: set[str] = set()
    shown_diary_ids: set[str] = set()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT id, author, content, attachments, created_at FROM moments "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor = _actor_name(r["author"], names)
            atts = _json_list(r["attachments"])
            shown_moment_ids.add(str(r["id"]))
            title = f"{actor} 发布了朋友圈"
            if atts:
                title += f"（{len(atts)}张图）"
            items.append(_timeline_item(r["created_at"], "moment", actor, title, _clip(r["content"]), r["id"], atts))

        cur = await db.execute(
            "SELECT id, author, title, content, created_at FROM diary_entries "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor = _actor_name(r["author"], names)
            shown_diary_ids.add(str(r["id"]))
            detail = r["title"] or _clip(r["content"])
            items.append(_timeline_item(r["created_at"], "diary", actor, f"{actor} 发布了日记", _clip(detail), r["id"]))

        cur = await db.execute(
            "SELECT id, image_path, message, created_at, sender FROM gifts "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor = _actor_name(r["sender"] or "aion", names)
            image_path = (r["image_path"] or "").strip()
            atts = [image_path if image_path.startswith("/uploads/") else f"/uploads/{image_path}"] if image_path else []
            items.append(_timeline_item(
                r["created_at"], "gift", actor,
                f"{actor} 给 {user_name} 送了礼物",
                _clip(r["message"]), r["id"], atts,
            ))

        cur = await db.execute(
            "SELECT id, actor, action, title, detail, target_type, target_id, result_type, result_id, metadata, created_at FROM idle_events "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor = _actor_name(r["actor"], names)
            title = _idle_event_timeline_title(r, actor, shown_diary_ids, shown_moment_ids)
            if not title:
                continue
            items.append(_timeline_item(
                r["created_at"], "idle_event", actor,
                title, _clip(r["detail"]), r["id"],
            ))

    status = load_location_status()
    changed_at = float(status.get("state_changed_at") or 0)
    if changed_at >= cutoff:
        state = status.get("state", "unknown")
        state_label = {"at_home": "在家", "outside": "外出", "unknown": "未知"}.get(state, state)
        detail_parts = []
        if status.get("address"):
            detail_parts.append(status.get("address"))
        dist = status.get("distance_from_home")
        if isinstance(dist, (int, float)) and dist >= 0 and state == "outside":
            detail_parts.append(f"距家约 {int(dist)} 米")
        items.append(_timeline_item(
            changed_at, "location", user_name,
            f"{user_name} 的位置状态变为{state_label}",
            "；".join(detail_parts),
        ))

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"items": items[:limit], "hours": hours, "total": len(items)}


@router.get("/api/activity/dates")
async def list_activity_dates():
    """返回所有有日志的日期"""
    return {"dates": get_available_dates()}


@router.get("/api/activity/logs/{date_str}")
async def get_activity_logs(date_str: str):
    """返回指定日期的活动日志"""
    entries = read_activity_logs(date_str)
    return {"entries": _resolve_entries(entries), "date": date_str}


@router.post("/api/activity/clear")
async def clear_all_activity_logs():
    """清除所有活动日志"""
    from activity import ACTIVITY_LOGS_DIR
    count = 0
    for f in ACTIVITY_LOGS_DIR.glob("*.jsonl"):
        f.unlink(missing_ok=True)
        count += 1
    return {"ok": True, "deleted": count}


@router.get("/api/activity/recent")
async def get_recent_activity(hours: int = KEEP_HOURS):
    """返回最近 N 小时的活动日志"""
    entries = read_recent_activity(hours)
    return {"entries": _resolve_entries(entries), "hours": hours}


@router.get("/api/activity/summary")
async def get_activity_summary(hours: int = KEEP_HOURS):
    """返回最近 N 小时的 10 分钟窗口活动摘要"""
    summaries = generate_activity_summary(hours)
    return {"summaries": summaries, "hours": hours}


# ── 活动追踪总开关 ──────────────────────────────────

@router.get("/api/activity/config")
async def get_activity_config():
    """获取活动追踪配置"""
    return {"activity_tracking_enabled": is_activity_tracking_enabled()}


class ActivityConfigUpdate(BaseModel):
    activity_tracking_enabled: bool


@router.put("/api/activity/config")
async def update_activity_config(body: ActivityConfigUpdate):
    """更新活动追踪配置"""
    set_activity_tracking_enabled(body.activity_tracking_enabled)
    await manager.broadcast({
        "type": "activity_config_changed",
        "data": {"activity_tracking_enabled": body.activity_tracking_enabled}
    })
    return {"ok": True, "activity_tracking_enabled": body.activity_tracking_enabled}
