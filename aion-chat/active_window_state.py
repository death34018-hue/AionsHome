"""Persist and restore the windows where the user most recently sent messages."""

import logging
import time

import aiosqlite

from database import get_db
from ws import manager


log = logging.getLogger("active_window_state")

AION_LAST_ACTIVE_KEY = "aion_last_active"
CONNOR_LAST_ACTIVE_KEY = "connor_last_active"


async def _upsert_many(values: dict[str, str]) -> None:
    now = time.time()
    async with get_db() as db:
        await db.executemany(
            "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(key, value, now) for key, value in values.items()],
        )
        await db.commit()


async def record_aion_private_active(*, manager_obj=manager) -> None:
    manager_obj.set_aion_last_active("private")
    try:
        await _upsert_many({AION_LAST_ACTIVE_KEY: "private"})
    except Exception:
        log.exception("Failed to persist Aion private active window")


async def record_chatroom_active(room_id: str, room_type: str, *, manager_obj=manager) -> None:
    room_id = (room_id or "").strip()
    room_type = (room_type or "").strip()
    if not room_id:
        return

    values: dict[str, str] = {}
    if room_type == "group":
        aion_target = f"chatroom:{room_id}"
        manager_obj.set_aion_last_active(aion_target)
        manager_obj.set_connor_last_active(room_id)
        values[AION_LAST_ACTIVE_KEY] = aion_target
        values[CONNOR_LAST_ACTIVE_KEY] = room_id
    elif room_type == "connor_1v1":
        manager_obj.set_connor_last_active(room_id)
        values[CONNOR_LAST_ACTIVE_KEY] = room_id
    else:
        return

    try:
        await _upsert_many(values)
    except Exception:
        log.exception("Failed to persist chatroom active window")


async def _room_type(db: aiosqlite.Connection, room_id: str) -> str | None:
    cur = await db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
    row = await cur.fetchone()
    return str(row[0]) if row else None


async def _infer_missing_routes(
    db: aiosqlite.Connection,
    rows: dict[str, str],
) -> dict[str, str]:
    inferred: dict[str, str] = {}

    if AION_LAST_ACTIVE_KEY not in rows:
        candidates: list[tuple[float, str]] = []
        cur = await db.execute(
            "SELECT MAX(created_at) FROM messages WHERE role='user'"
        )
        private_row = await cur.fetchone()
        if private_row and private_row[0] is not None:
            candidates.append((float(private_row[0]), "private"))

        cur = await db.execute(
            "SELECT m.room_id, MAX(m.created_at) "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id=m.room_id "
            "WHERE m.sender='user' AND r.type='group' "
            "GROUP BY m.room_id ORDER BY MAX(m.created_at) DESC LIMIT 1"
        )
        group_row = await cur.fetchone()
        if group_row:
            candidates.append((float(group_row[1]), f"chatroom:{group_row[0]}"))

        if candidates:
            inferred[AION_LAST_ACTIVE_KEY] = max(candidates, key=lambda item: item[0])[1]

    if CONNOR_LAST_ACTIVE_KEY not in rows:
        cur = await db.execute(
            "SELECT m.room_id, MAX(m.created_at) "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id=m.room_id "
            "WHERE m.sender='user' AND r.type IN ('group','connor_1v1') "
            "GROUP BY m.room_id ORDER BY MAX(m.created_at) DESC LIMIT 1"
        )
        connor_row = await cur.fetchone()
        if connor_row:
            inferred[CONNOR_LAST_ACTIVE_KEY] = str(connor_row[0])

    if inferred:
        now = time.time()
        await db.executemany(
            "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(key, value, now) for key, value in inferred.items()],
        )
        await db.commit()
        rows.update(inferred)

    return rows


async def restore_active_windows(*, manager_obj=manager) -> dict[str, str | None]:
    restored: dict[str, str | None] = {
        AION_LAST_ACTIVE_KEY: "private",
        CONNOR_LAST_ACTIVE_KEY: None,
    }
    manager_obj.set_aion_last_active("private")
    manager_obj.set_connor_last_active(None)

    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT key, value FROM runtime_state WHERE key IN (?, ?)",
                (AION_LAST_ACTIVE_KEY, CONNOR_LAST_ACTIVE_KEY),
            )
            rows = {str(row[0]): str(row[1]) for row in await cur.fetchall()}
            rows = await _infer_missing_routes(db, rows)
            stale_keys: list[str] = []

            aion_value = rows.get(AION_LAST_ACTIVE_KEY)
            if aion_value == "private":
                restored[AION_LAST_ACTIVE_KEY] = "private"
            elif aion_value and aion_value.startswith("chatroom:"):
                room_id = aion_value.split(":", 1)[1]
                if await _room_type(db, room_id) == "group":
                    manager_obj.set_aion_last_active(aion_value)
                    restored[AION_LAST_ACTIVE_KEY] = aion_value
                else:
                    stale_keys.append(AION_LAST_ACTIVE_KEY)
            elif aion_value:
                stale_keys.append(AION_LAST_ACTIVE_KEY)

            connor_value = rows.get(CONNOR_LAST_ACTIVE_KEY)
            if connor_value:
                if await _room_type(db, connor_value) in {"group", "connor_1v1"}:
                    manager_obj.set_connor_last_active(connor_value)
                    restored[CONNOR_LAST_ACTIVE_KEY] = connor_value
                else:
                    stale_keys.append(CONNOR_LAST_ACTIVE_KEY)

            if stale_keys:
                await db.executemany(
                    "DELETE FROM runtime_state WHERE key=?",
                    [(key,) for key in stale_keys],
                )
                await db.commit()
    except Exception:
        log.exception("Failed to restore active windows; using safe defaults")

    return restored
