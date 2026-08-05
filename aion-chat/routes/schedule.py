"""
日程路由：列表 / 手动添加 / 删除
"""

import time
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

import aiosqlite
from database import get_db
from schedule import get_schedule_origin_name
from schedule_history import fetch_schedule_history, finish_schedule
from ws import manager

router = APIRouter()


class ScheduleCreate(BaseModel):
    type: str = "alarm"          # alarm / reminder
    trigger_at: str              # ISO: 2026-03-25T10:00
    content: str


@router.get("/api/schedules")
async def list_schedules(status: Optional[str] = Query(None)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if status == "history":
            rows = await fetch_schedule_history(db)
        elif status:
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status=? ORDER BY trigger_at", (status,)
            )
            rows = [dict(r) for r in await cur.fetchall()]
        else:
            cur = await db.execute("SELECT * FROM schedules ORDER BY trigger_at")
            rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row["origin_name"] = get_schedule_origin_name(row.get("origin"))
        return rows


@router.post("/api/schedules")
async def create_schedule(body: ScheduleCreate):
    sid = f"sch_{int(time.time()*1000)}"
    now = time.time()
    trigger_at = body.trigger_at.replace("T", " ")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status, origin, origin_room_id) VALUES (?,?,?,?,?,?,?,?)",
            (sid, body.type, trigger_at, body.content, now, "active", "user", ""),
        )
        await db.commit()
    item = {"id": sid, "type": body.type, "trigger_at": trigger_at,
            "content": body.content, "created_at": now, "status": "active",
            "origin": "user", "origin_room_id": "", "origin_name": get_schedule_origin_name("user")}
    await manager.broadcast({"type": "schedule_changed"})
    return item


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    async with get_db() as db:
        changed = await finish_schedule(db, schedule_id, "cancelled")
        await db.commit()
    if changed:
        await manager.broadcast({"type": "schedule_changed"})
    return {"ok": changed}
