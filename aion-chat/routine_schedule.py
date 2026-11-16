"""Weekly personal routines and date-only edits, with optional linked alarms."""

import json
import uuid
from datetime import date

from database import get_db


_DEFAULT_DAY = [
    ("morning", "09:00", "10:00", "起床、洗漱、煮茶做早饭"),
    ("walk1", "10:00", "10:30", "早间遛狗、活动"),
    ("work1", "10:30", "13:00", "工作块一"),
    ("lunch", "13:00", "14:30", "午餐、午间休息"),
    ("work2", "14:30", "15:30", "工作块二"),
    ("gym", "15:30", "17:00", "健身"),
    ("walk2", "17:00", "18:30", "第二次遛狗、自由活动、洗澡"),
    ("dinner", "18:30", "19:30", "做饭、吃晚餐"),
    ("free", "19:30", "21:30", "自由活动"),
    ("wash", "21:30", "22:00", "洗漱"),
    ("nest", "22:00", "23:30", "回窝、准备睡觉"),
    ("sleep", "23:30", "", "入睡"),
]
_TRAINING = ["健身 · 练胸", "健身 · 练背", "拉伸", "健身 · 练腿", "休息 / 随意有氧", "休息 / 随意有氧", "休息 / 随意有氧"]


async def init_routine_storage():
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS routine_weekly (
                weekday INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 6),
                items TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS routine_overrides (
                date TEXT PRIMARY KEY,
                items TEXT NOT NULL
            );
        """)
        from calendar_store import ensure_calendar_schema
        await ensure_calendar_schema(db)
        for weekday in range(7):
            items = []
            for item_id, start, end, content in _DEFAULT_DAY:
                if item_id == "gym":
                    content = _TRAINING[weekday]
                elif weekday >= 5 and item_id.startswith("work"):
                    content = "自由活动"
                items.append({"id": item_id, "start": start, "end": end, "content": content})
            # Empty lists remain rows, so deleting everything never reseeds it.
            await db.execute("INSERT OR IGNORE INTO routine_weekly VALUES (?, ?)", (weekday, _json(items)))
        await db.commit()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _weekly(db, weekday):
    cursor = await db.execute("SELECT items FROM routine_weekly WHERE weekday=?", (weekday,))
    row = await cursor.fetchone()
    return json.loads(row[0]) if row else []


async def _overrides(db, day):
    cursor = await db.execute("SELECT items FROM routine_overrides WHERE date=?", (day.isoformat(),))
    row = await cursor.fetchone()
    return json.loads(row[0]) if row else {}


async def _save_weekly(db, weekday, items):
    await db.execute("INSERT OR REPLACE INTO routine_weekly VALUES (?, ?)", (weekday, _json(items)))


async def _save_overrides(db, day, items):
    if items:
        await db.execute("INSERT OR REPLACE INTO routine_overrides VALUES (?, ?)", (day.isoformat(), _json(items)))
    else:
        await db.execute("DELETE FROM routine_overrides WHERE date=?", (day.isoformat(),))


def _merge(weekly, overrides):
    result = {row["id"]: {**row, "source": "weekly", "overridden": False} for row in weekly}
    for item_id, row in overrides.items():
        if row is None:
            result.pop(item_id, None)
        elif row["origin"] != "weekly" or item_id in result:
            result[item_id] = {k: row[k] for k in ("id", "start", "end", "content")}
            result[item_id].update(source=row["origin"], overridden=True)
    return sorted(result.values(), key=lambda row: (row["start"], row["id"]))


async def get_routine(day: date):
    async with get_db() as db:
        weekly = await _weekly(db, day.weekday())
        overrides = await _overrides(db, day)
    return {
        "date": day.isoformat(), "weekday": day.weekday(),
        "weekly_items": sorted(weekly, key=lambda row: (row["start"], row["id"])),
        "items": _merge(weekly, overrides), "has_overrides": bool(overrides),
    }


async def save_item(day: date, scope: str, values: dict, item_id=None, *, apply_to_date=False, remind=None, reminder_at=""):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        weekly = await _weekly(db, day.weekday())
        overrides = await _overrides(db, day)
        current = {row["id"]: row for row in _merge(weekly, overrides)}
        weekly_ids = {row["id"] for row in weekly}
        if item_id and item_id not in (weekly_ids if scope == "weekly" else current):
            # A date-only addition can be promoted to a weekly item explicitly.
            if scope != "weekly" or item_id not in current:
                raise KeyError(item_id)
        item_id = item_id or uuid.uuid4().hex
        item = {"id": item_id, **values}
        if scope == "weekly":
            was_weekly = item_id in weekly_ids
            weekly = [row for row in weekly if row["id"] != item_id] + [item]
            await _save_weekly(db, day.weekday(), weekly)
            if (not was_weekly or apply_to_date) and item_id in overrides:
                del overrides[item_id]
                await _save_overrides(db, day, overrides)
        else:
            overrides[item_id] = {**item, "origin": "weekly" if item_id in weekly_ids else "day"}
            await _save_overrides(db, day, overrides)
        if remind is not None:
            from calendar_store import set_routine_reminder
            await set_routine_reminder(db, day, item, remind, reminder_at)
        await db.commit()
    return item


async def delete_item(day: date, scope: str, item_id: str):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        weekly = await _weekly(db, day.weekday())
        overrides = await _overrides(db, day)
        if scope == "weekly":
            if not any(row["id"] == item_id for row in weekly):
                raise KeyError(item_id)
            await _save_weekly(db, day.weekday(), [row for row in weekly if row["id"] != item_id])
        else:
            if not any(row["id"] == item_id for row in _merge(weekly, overrides)):
                raise KeyError(item_id)
            if any(row["id"] == item_id for row in weekly):
                overrides[item_id] = None
            else:
                overrides.pop(item_id, None)
            await _save_overrides(db, day, overrides)
        from calendar_store import cancel_routine_links
        await cancel_routine_links(db, item_id, day=day if scope == "day" else None, weekday=day.weekday() if scope == "weekly" else None)
        await db.commit()


async def copy_week(source_weekday: int, target_weekdays: list[int]):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        source = await _weekly(db, source_weekday)
        for weekday in target_weekdays:
            from calendar_store import cancel_routine_links
            removed_ids = {row["id"] for row in await _weekly(db, weekday)} - {row["id"] for row in source}
            for item_id in removed_ids:
                await cancel_routine_links(db, item_id, weekday=weekday)
            # IDs are scoped by weekday, so copied lists are independently editable.
            await _save_weekly(db, weekday, source)
        await db.commit()


async def reset_date(day: date):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        from calendar_store import cancel_routine_links
        extra_ids = set(await _overrides(db, day)) - {row["id"] for row in await _weekly(db, day.weekday())}
        for item_id in extra_ids:
            await cancel_routine_links(db, item_id, day=day)
        await db.execute("DELETE FROM routine_overrides WHERE date=?", (day.isoformat(),))
        await db.commit()
