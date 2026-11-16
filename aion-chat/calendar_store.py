"""Shared reminder identity and metadata; the existing scheduler owns delivery."""

import hashlib
import re
import time
import unicodedata
import uuid
from datetime import date, datetime

import aiosqlite
from schedule_history import finish_schedule


async def ensure_calendar_schema(db):
    await db.execute("CREATE TABLE IF NOT EXISTS calendar_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    await db.execute("INSERT OR IGNORE INTO calendar_settings VALUES ('sharing', '1')")
    await db.execute("""CREATE TABLE IF NOT EXISTS schedule_event_links (
        schedule_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, event_at TEXT NOT NULL DEFAULT ''
    )""")


async def table_exists(db, name):
    return await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))).fetchone() is not None


async def sharing_enabled(db):
    if not await table_exists(db, "calendar_settings"):
        return True
    row = await (await db.execute("SELECT value FROM calendar_settings WHERE key='sharing'")).fetchone()
    return row is None or row[0] == "1"


def normalize_time(value):
    return datetime.fromisoformat(str(value).replace("T", " ")).strftime("%Y-%m-%d %H:%M")


def content_key(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


async def active_records(db, *, include_ended=False):
    if not await table_exists(db, "schedules"):
        return []
    db.row_factory = aiosqlite.Row
    status_clause = "status IN ('active','triggered','cancelled')" if include_ended else "status='active'"
    rows = [dict(row) for row in await (await db.execute(
        f"SELECT * FROM schedules WHERE {status_clause} AND type IN ('alarm','reminder','monitor') ORDER BY trigger_at, created_at, id"
    )).fetchall()]
    links = {}
    if await table_exists(db, "schedule_event_links"):
        links = {row["schedule_id"]: dict(row) for row in await (await db.execute("SELECT * FROM schedule_event_links")).fetchall()}
    for row in rows:
        link = links.get(row["id"], {})
        row["event_key"] = link.get("event_key") or "schedule:" + row["id"]
        row["event_at"] = link.get("event_at", "")
    return rows


async def write_schedule(db, stype, trigger_at, content, origin="aion", origin_room_id="", *, event_key="", event_at="", item_id=None):
    if not db.in_transaction:
        await db.execute("BEGIN IMMEDIATE")
    await ensure_calendar_schema(db)
    trigger_at = normalize_time(trigger_at)
    content = content.strip()
    if not content:
        raise ValueError("日程内容不能为空")
    event_at = normalize_time(event_at) if event_at else ""
    if event_key.startswith("routine:"):
        _, linked_date, linked_id = event_key.split(":", 2)
        date.fromisoformat(linked_date)
        if not linked_id:
            raise ValueError("关联安排缺少编号")
    if event_at and not event_key:
        digest = hashlib.sha256(content_key(content).encode()).hexdigest()[:16]
        event_key = f"event:{event_at.replace(' ', 'T')}:{digest}"
    records = await active_records(db)
    original = next((row for row in records if row["id"] == item_id), None)
    if item_id and original is None:
        raise KeyError(item_id)
    if original:
        event_key = event_key or original["event_key"]
        event_at = event_at or original["event_at"]
    duplicate = None
    for row in records:
        if row["id"] == item_id or row["type"] != stype or row["trigger_at"].replace("T", " ")[:16] != trigger_at:
            continue
        linked = event_key and row["event_key"] == event_key
        different_events = (event_key and not event_key.startswith("schedule:")
                            and not row["event_key"].startswith("schedule:") and row["event_key"] != event_key)
        if linked or (not different_events and content_key(row["content"]) == content_key(content)):
            duplicate = row
            break
    if duplicate:
        if original:
            await finish_schedule(db, original["id"], "cancelled")
        sid = duplicate["id"]
        if event_key and event_key != "schedule:" + sid:
            await db.execute("INSERT OR REPLACE INTO schedule_event_links VALUES (?, ?, ?)",
                             (sid, event_key, event_at or duplicate["event_at"]))
            if event_at:
                await db.execute("UPDATE schedule_event_links SET event_at=? WHERE event_key=?", (event_at, event_key))
        return {**duplicate, "event_key": event_key or duplicate["event_key"],
                "event_at": event_at or duplicate["event_at"], "created": False, "updated": False}
    sid = item_id or "sch_" + uuid.uuid4().hex
    created_at = original["created_at"] if original else time.time()
    if original:
        await db.execute("UPDATE schedules SET type=?, trigger_at=?, content=? WHERE id=? AND status='active'", (stype, trigger_at, content, sid))
    else:
        await db.execute("INSERT INTO schedules (id,type,trigger_at,content,created_at,status,origin,origin_room_id) VALUES (?,?,?,?,?,'active',?,?)",
                         (sid, stype, trigger_at, content, created_at, origin, origin_room_id))
    await db.execute("INSERT OR REPLACE INTO schedule_event_links VALUES (?, ?, ?)", (sid, event_key or "schedule:" + sid, event_at))
    if event_at and event_key:
        await db.execute("UPDATE schedule_event_links SET event_at=? WHERE event_key=?", (event_at, event_key))
        if event_key.startswith("schedule:"):
            parent_id = event_key[len("schedule:"):]
            if any(row["id"] == parent_id for row in records):
                await db.execute("INSERT OR REPLACE INTO schedule_event_links VALUES (?, ?, ?)", (parent_id, event_key, event_at))
    return {"id": sid, "type": stype, "trigger_at": trigger_at, "content": content, "created_at": created_at,
            "origin": original["origin"] if original else origin,
            "origin_room_id": original["origin_room_id"] if original else origin_room_id,
            "event_key": event_key or "schedule:" + sid, "event_at": event_at,
            "status": "active", "created": not bool(original), "updated": bool(original)}


async def set_routine_reminder(db, day, item, enabled, trigger_at=""):
    await ensure_calendar_schema(db)
    key = f"routine:{day.isoformat()}:{item['id']}"
    linked = [row for row in await active_records(db) if row["event_key"] == key and row["type"] == "alarm"]
    if not enabled:
        for row in linked:
            await finish_schedule(db, row["id"], "cancelled")
        return
    return await write_schedule(db, "alarm", trigger_at, item["content"], "user",
                                event_key=key, event_at=f"{day.isoformat()} {item['start']}",
                                item_id=linked[0]["id"] if linked else None)


async def cancel_routine_links(db, item_id, *, day=None, weekday=None):
    for record in await active_records(db):
        parts = record["event_key"].split(":", 2)
        if len(parts) != 3 or parts[0] != "routine" or parts[2] != item_id:
            continue
        when = date.fromisoformat(parts[1])
        if (day is not None and when == day) or (weekday is not None and when.weekday() == weekday):
            await finish_schedule(db, record["id"], "cancelled")
