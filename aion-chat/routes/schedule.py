"""
日程路由：列表 / 手动添加 / 删除
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Literal, Optional

import aiosqlite
from database import get_db
from schedule import get_schedule_origin_name
from schedule_history import fetch_schedule_history, finish_schedule
from ws import manager
from calendar_store import write_schedule

router = APIRouter()


class ScheduleCreate(BaseModel):
    type: Literal["alarm", "reminder", "monitor"] = "alarm"
    trigger_at: str              # ISO: 2026-03-25T10:00
    content: str
    event_key: str = ""
    event_at: str = ""


@router.get("/api/schedules")
async def list_schedules(status: Optional[str] = Query(None)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if status == "history":
            rows = await fetch_schedule_history(db)
        elif status:
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status=? AND type!='proactive' ORDER BY trigger_at", (status,)
            )
            rows = [dict(r) for r in await cur.fetchall()]
        else:
            cur = await db.execute("SELECT * FROM schedules WHERE type!='proactive' ORDER BY trigger_at")
            rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row["origin_name"] = get_schedule_origin_name(row.get("origin"))
        return rows


@router.post("/api/schedules")
async def create_schedule(body: ScheduleCreate):
    async with get_db() as db:
        try:
            item = await write_schedule(db, body.type, body.trigger_at, body.content, "user", event_key=body.event_key, event_at=body.event_at)
        except ValueError as error:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail=str(error)) from None
        await db.commit()
    item["origin_name"] = get_schedule_origin_name(item.get("origin"))
    await manager.broadcast({"type": "schedule_changed"})
    return item


@router.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleCreate):
    from fastapi import HTTPException
    async with get_db() as db:
        try:
            item = await write_schedule(db, body.type, body.trigger_at, body.content, "user", event_key=body.event_key, event_at=body.event_at, item_id=schedule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="这条提醒已不存在或已经结束") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        await db.commit()
    item["origin_name"] = get_schedule_origin_name(item.get("origin"))
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
