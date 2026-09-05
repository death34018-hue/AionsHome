"""Small SQLite store for private autonomous-journey keepsakes."""

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import get_db


ACTORS = ("aion", "connor")


async def ensure_autonomy_niche_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_niche_cards (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            title TEXT NOT NULL,
            reflection TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            photo_path TEXT DEFAULT '',
            image_prompt TEXT DEFAULT '',
            action_trace TEXT DEFAULT '[]',
            shared INTEGER NOT NULL DEFAULT 0,
            sources TEXT DEFAULT '[]',
            family_event_id TEXT DEFAULT '',
            mentioned_at REAL,
            created_at REAL NOT NULL
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(autonomy_niche_cards)")
    columns = {str(row[1]) for row in await cursor.fetchall()}
    if "mentioned_at" not in columns:
        await db.execute("ALTER TABLE autonomy_niche_cards ADD COLUMN mentioned_at REAL")
    if "photo_paths" not in columns:
        await db.execute("ALTER TABLE autonomy_niche_cards ADD COLUMN photo_paths TEXT DEFAULT '[]'")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_niches_actor "
        "ON autonomy_niche_cards(actor, created_at DESC)"
    )


def _actor(value: str) -> str:
    actor = str(value or "").strip().lower()
    if actor not in ACTORS:
        raise ValueError("unknown actor")
    return actor


def _clip(value, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _texts(values, *, count: int, length: int) -> list[str]:
    result = []
    for value in values or []:
        text = _clip(value, length)
        if text and text not in result:
            result.append(text)
        if len(result) >= count:
            break
    return result


def _sources(values) -> list[dict]:
    result = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        title = _clip(value.get("title"), 120)
        url = _clip(value.get("url"), 800)
        if url:
            result.append({"title": title or "旅行线索", "url": url})
        if len(result) >= 3:
            break
    return result


@asynccontextmanager
async def _connection(db=None):
    if db is not None:
        yield db
        return
    async with get_db() as connection:
        yield connection


def _row(row) -> dict:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "actor": row["actor"],
        "title": row["title"],
        "reflection": row["reflection"] or "",
        "tags": json.loads(row["tags"] or "[]"),
        "photo_path": row["photo_path"] or "",
        "photo_paths": json.loads(row["photo_paths"] or "[]"),
        "image_prompt": row["image_prompt"] or "",
        "action_trace": json.loads(row["action_trace"] or "[]"),
        "shared": bool(row["shared"]),
        "sources": json.loads(row["sources"] or "[]"),
        "family_event_id": row["family_event_id"] or "",
        "mentioned": row["mentioned_at"] is not None,
        "mentioned_at": (
            float(row["mentioned_at"]) if row["mentioned_at"] is not None else None
        ),
        "created_at": float(row["created_at"]),
    }


async def create_niche_card(
    *,
    actor: str,
    session_id: str,
    title: str,
    reflection: str = "",
    tags=None,
    photo_path: str = "",
    image_prompt: str = "",
    action_trace=None,
    shared: bool = False,
    sources=None,
    family_event_id: str = "",
    created_at: float | None = None,
    db=None,
) -> dict:
    actor = _actor(actor)
    session_id = _clip(session_id, 120)
    reflection = _clip(reflection, 6000)
    photo_path = _clip(photo_path, 800)
    if not session_id:
        raise ValueError("session_id is required")
    if not reflection and not photo_path:
        raise ValueError("a niche card needs a reflection or photo")
    timestamp = float(created_at if created_at is not None else time.time())
    card_id = f"niche_{int(timestamp * 1000)}_{time.time_ns() % 100000}"
    normalized_tags = _texts(tags, count=3, length=24)
    normalized_trace = _texts(action_trace, count=3, length=180)
    normalized_sources = _sources(sources)
    values = (
        card_id, session_id, actor, _clip(title, 120) or "一次小旅行",
        reflection, json.dumps(normalized_tags, ensure_ascii=False), photo_path,
        _clip(image_prompt, 2000), json.dumps(normalized_trace, ensure_ascii=False),
        int(bool(shared)), json.dumps(normalized_sources, ensure_ascii=False),
        _clip(family_event_id, 120), timestamp,
    )
    async with _connection(db) as connection:
        await ensure_autonomy_niche_tables(connection)
        await connection.execute(
            "INSERT INTO autonomy_niche_cards "
            "(id, session_id, actor, title, reflection, tags, photo_path, image_prompt, "
            "action_trace, shared, sources, family_event_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        await connection.commit()
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT * FROM autonomy_niche_cards WHERE id=?", (card_id,)
        )
        return _row(await cursor.fetchone())


async def archive_album_event(event: dict, db) -> str | None:
    """Archive a completed album reflection in the caller's event transaction."""
    meta = event.get("metadata") or {}
    if (event.get("action") != "wake_summary" or not isinstance(meta, dict)
            or meta.get("selected_action") != "album_browse"
            or meta.get("outcome", "finished") != "finished"
            or event.get("actor") not in ACTORS or event.get("result_type") == "niche_card"):
        return None
    photos = meta.get("album_photos")
    paths = [p["url"] for p in (photos[:2] if isinstance(photos, list) else [])
             if isinstance(p, dict) and isinstance(p.get("url"), str)
             and p["url"].startswith("/uploads/album/")]
    reflection = meta.get("album_reflection") or event.get("detail") or ""
    if not paths or not isinstance(reflection, str) or not reflection.strip():
        return None
    await ensure_autonomy_niche_tables(db)
    card_id = "niche_album_" + event["id"]
    session_id = meta.get("session_id") or "album_" + event["id"]
    await db.execute(
        "INSERT INTO autonomy_niche_cards "
        "(id,session_id,actor,title,reflection,tags,photo_path,photo_paths,shared,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING",
        (card_id, session_id, event["actor"], event["title"], reflection,
         json.dumps(["相册随想"], ensure_ascii=False), paths[0], json.dumps(paths),
         int(meta.get("shared") is True), event["created_at"]))
    cursor = await db.execute("SELECT id FROM autonomy_niche_cards WHERE session_id=?", (session_id,))
    card_id = (await cursor.fetchone())[0]
    # 保留已归档标记：手动删除壁龛卡片后，历史补录不会把它重新放回来。
    await db.execute("UPDATE idle_events SET result_type='niche_card',result_id=? WHERE id=?",
                     (card_id, event["id"]))
    event.update(result_type="niche_card", result_id=card_id)
    return card_id


async def backfill_album_niche_cards(db) -> int:
    """Backfill saved album events only; never call a model or send a message."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM idle_events WHERE action='wake_summary' "
        "AND COALESCE(result_type,'')!='niche_card' AND metadata LIKE '%album_browse%'")
    count = 0
    for row in await cursor.fetchall():
        event = dict(row)
        try:
            event["metadata"] = json.loads(event.get("metadata") or "{}")
        except (ValueError, TypeError):
            continue
        if await archive_album_event(event, db):
            count += 1
    return count


async def list_niche_cards(actor: str, limit: int = 60, *, db=None) -> list[dict]:
    actor = _actor(actor)
    limit = max(1, min(int(limit or 60), 200))
    async with _connection(db) as connection:
        await ensure_autonomy_niche_tables(connection)
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT * FROM autonomy_niche_cards WHERE actor=? "
            "ORDER BY created_at DESC LIMIT ?",
            (actor, limit),
        )
        return [_row(row) for row in await cursor.fetchall()]


async def mark_niche_card_mentioned(
    card_id: str,
    *,
    mentioned_at: float | None = None,
    db=None,
) -> dict | None:
    timestamp = float(mentioned_at if mentioned_at is not None else time.time())
    async with _connection(db) as connection:
        await ensure_autonomy_niche_tables(connection)
        await connection.execute(
            "UPDATE autonomy_niche_cards SET mentioned_at=? WHERE id=?",
            (timestamp, str(card_id or "").strip()),
        )
        await connection.commit()
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT * FROM autonomy_niche_cards WHERE id=?",
            (str(card_id or "").strip(),),
        )
        row = await cursor.fetchone()
        return _row(row) if row else None


async def set_niche_card_mentioned(
    actor: str,
    card_id: str,
    mentioned: bool,
    *,
    mentioned_at: float | None = None,
    db=None,
) -> dict | None:
    actor = _actor(actor)
    card_id = str(card_id or "").strip()
    if not card_id:
        return None
    timestamp = (
        float(mentioned_at if mentioned_at is not None else time.time())
        if mentioned else None
    )
    async with _connection(db) as connection:
        await ensure_autonomy_niche_tables(connection)
        await connection.execute(
            "UPDATE autonomy_niche_cards SET mentioned_at=? WHERE actor=? AND id=?",
            (timestamp, actor, card_id),
        )
        await connection.commit()
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT * FROM autonomy_niche_cards WHERE actor=? AND id=?",
            (actor, card_id),
        )
        row = await cursor.fetchone()
        return _row(row) if row else None


async def delete_niche_card(actor: str, card_id: str, *, db=None) -> bool:
    actor = _actor(actor)
    card_id = str(card_id or "").strip()
    if not card_id:
        return False
    async with _connection(db) as connection:
        await ensure_autonomy_niche_tables(connection)
        cursor = await connection.execute(
            "DELETE FROM autonomy_niche_cards WHERE actor=? AND id=?",
            (actor, card_id),
        )
        await connection.commit()
        return cursor.rowcount > 0


async def recent_niche_index(actor: str, limit: int = 6, *, db=None) -> list[dict]:
    cards = await list_niche_cards(actor, max(1, min(int(limit or 6), 6)), db=db)
    return [
        {
            "date": (
                datetime(1970, 1, 1, tzinfo=timezone.utc)
                + timedelta(seconds=card["created_at"])
            ).astimezone().strftime("%Y-%m-%d"),
            "title": card["title"],
            "tags": card["tags"],
        }
        for card in cards
    ]
