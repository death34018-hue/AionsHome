"""Weekly personal routines and date-only edits, with optional linked alarms."""

import json
import logging
import uuid
from datetime import date
from pathlib import Path

from config import DEFAULT_ROUTINE_PATH
from database import get_db

logger = logging.getLogger(__name__)


def _normalize_default_items(raw_items):
    """Normalize optional local default routine rows into stored item dicts."""
    if not isinstance(raw_items, list):
        return []
    items = []
    for row in raw_items:
        if isinstance(row, dict):
            content = str(row.get("content") or "").strip()
            start = str(row.get("start") or "").strip()
            end = str(row.get("end") or "").strip()
            item_id = str(row.get("id") or "").strip() or uuid.uuid4().hex
        elif isinstance(row, (list, tuple)) and len(row) == 4:
            item_id = str(row[0] or "").strip() or uuid.uuid4().hex
            start = str(row[1] or "").strip()
            end = str(row[2] or "").strip()
            content = str(row[3] or "").strip()
        else:
            continue
        if content and start:
            items.append({"id": item_id, "start": start, "end": end, "content": content})
    return items


def _load_default_routines():
    """Load per-weekday defaults from an untracked local file, or start empty."""
    empty = {weekday: [] for weekday in range(7)}
    path = Path(DEFAULT_ROUTINE_PATH)
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read default routine file %s: %s", path, exc)
        return empty
    if isinstance(raw, dict):
        fallback = raw.get("default", [])
        return {
            weekday: _normalize_default_items(raw.get(str(weekday), raw.get(weekday, fallback)))
            for weekday in range(7)
        }
    if isinstance(raw, list):
        if len(raw) == 7 and all(isinstance(day_items, list) for day_items in raw):
            return {weekday: _normalize_default_items(raw[weekday]) for weekday in range(7)}
        shared = _normalize_default_items(raw)
        return {weekday: list(shared) for weekday in range(7)}
    return empty


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
        defaults = _load_default_routines()
        for weekday in range(7):
            # Empty lists remain rows, so deleting everything never reseeds it.
            await db.execute("INSERT OR IGNORE INTO routine_weekly VALUES (?, ?)", (weekday, _json(defaults[weekday])))
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
