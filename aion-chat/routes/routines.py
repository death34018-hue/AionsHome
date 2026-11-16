"""Routine CRUD, unified calendar view, and context-sharing preference."""

import re
from datetime import date as Date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

import routine_schedule as store
from ws import manager

router = APIRouter()
Scope = Literal["weekly", "day"]
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class RoutineWrite(BaseModel):
    date: Date
    scope: Scope = "weekly"
    apply_to_date: bool = False
    content: str = Field(max_length=500)
    start: str
    end: str = ""
    remind: bool | None = None
    reminder_at: str = ""

    @model_validator(mode="after")
    def valid_reminder(self):
        if self.remind is not None and self.scope != "day":
            raise ValueError("主动提醒请在具体日期设置")
        if self.remind:
            from calendar_store import normalize_time
            self.reminder_at = normalize_time(self.reminder_at)
        return self

    @field_validator("content")
    @classmethod
    def clean_content(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请填写安排内容")
        return value

    @field_validator("start", "end")
    @classmethod
    def valid_time(cls, value, info):
        if info.field_name == "end" and value == "":
            return value
        if not _TIME.fullmatch(value):
            raise ValueError("时间格式应为 HH:MM，例如 09:00")
        return value


class RoutineCopy(BaseModel):
    source_weekday: int = Field(ge=0, le=6)
    target_weekdays: list[int] = Field(min_length=1, max_length=7)

    @field_validator("target_weekdays")
    @classmethod
    def valid_weekdays(cls, values):
        if any(day < 0 or day > 6 for day in values):
            raise ValueError("请选择有效星期")
        return sorted(set(values))


async def _changed():
    await manager.broadcast({"type": "routine_changed"})


@router.get("/api/routines")
async def get_routine(date: Date):
    return await store.get_routine(date)


@router.get("/api/routines/timeline")
async def get_timeline(date: Date):
    from schedule_timeline import get_timeline
    return await get_timeline(date, db_factory=store.get_db)


class SharingUpdate(BaseModel):
    enabled: bool


@router.put("/api/routines/sharing")
async def update_sharing(body: SharingUpdate):
    from calendar_store import ensure_calendar_schema
    async with store.get_db() as db:
        await ensure_calendar_schema(db)
        await db.execute("UPDATE calendar_settings SET value=? WHERE key='sharing'", ("1" if body.enabled else "0",))
        await db.commit()
    await _changed()
    return {"enabled": body.enabled}


@router.post("/api/routines/items")
async def create_item(body: RoutineWrite):
    item = await store.save_item(body.date, body.scope, body.model_dump(include={"content", "start", "end"}), remind=body.remind, reminder_at=body.reminder_at)
    await _changed()
    if body.remind is not None:
        await manager.broadcast({"type": "schedule_changed"})
    return item


@router.put("/api/routines/items/{item_id}")
async def update_item(item_id: str, body: RoutineWrite):
    try:
        item = await store.save_item(body.date, body.scope, body.model_dump(include={"content", "start", "end"}), item_id, apply_to_date=body.apply_to_date, remind=body.remind, reminder_at=body.reminder_at)
    except KeyError:
        raise HTTPException(status_code=404, detail="这项安排已不存在，请刷新后重试") from None
    await _changed()
    if body.remind is not None:
        await manager.broadcast({"type": "schedule_changed"})
    return item


@router.delete("/api/routines/items/{item_id}")
async def delete_item(item_id: str, date: Date, scope: Scope):
    try:
        await store.delete_item(date, scope, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="这项安排已不存在，请刷新后重试") from None
    await _changed()
    await manager.broadcast({"type": "schedule_changed"})
    return {"ok": True}


@router.post("/api/routines/copy")
async def copy_routine(body: RoutineCopy):
    targets = [day for day in body.target_weekdays if day != body.source_weekday]
    if not targets:
        raise HTTPException(status_code=422, detail="请选择其他星期")
    await store.copy_week(body.source_weekday, targets)
    await _changed()
    await manager.broadcast({"type": "schedule_changed"})
    return {"ok": True}


@router.delete("/api/routines/overrides/{date}")
async def reset_routine(date: Date):
    await store.reset_date(date)
    await _changed()
    await manager.broadcast({"type": "schedule_changed"})
    return {"ok": True}
