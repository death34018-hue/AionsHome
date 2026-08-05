"""Schedule history lifecycle, ordering, and bounded retention."""

import time
import sqlite3
from datetime import datetime


HISTORY_LIMIT = 30
HISTORY_STATUSES = ("triggered", "cancelled")


def _legacy_ended_at(row: dict) -> float:
    if row["status"] == "triggered":
        try:
            return datetime.fromisoformat(
                str(row["trigger_at"]).replace("T", " ")
            ).timestamp()
        except (TypeError, ValueError):
            pass
    return float(row["created_at"])


async def trim_schedule_history(db, limit: int = HISTORY_LIMIT) -> None:
    await db.execute(
        """
        DELETE FROM schedules
        WHERE status IN ('triggered', 'cancelled')
          AND id NOT IN (
              SELECT id
              FROM schedules
              WHERE status IN ('triggered', 'cancelled')
              ORDER BY ended_at DESC, created_at DESC, id DESC
              LIMIT ?
          )
        """,
        (limit,),
    )


async def migrate_schedule_history(db) -> None:
    try:
        await db.execute("ALTER TABLE schedules ADD COLUMN ended_at REAL")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise

    cursor = await db.execute(
        """
        SELECT id, trigger_at, created_at, status
        FROM schedules
        WHERE status IN ('triggered', 'cancelled') AND ended_at IS NULL
        """
    )
    columns = [item[0] for item in cursor.description]
    for values in await cursor.fetchall():
        row = dict(zip(columns, values))
        await db.execute(
            "UPDATE schedules SET ended_at=? WHERE id=? AND ended_at IS NULL",
            (_legacy_ended_at(row), row["id"]),
        )
    await trim_schedule_history(db)


async def finish_schedule(
    db,
    schedule_id: str,
    status: str,
    ended_at: float | None = None,
) -> bool:
    if status not in HISTORY_STATUSES:
        raise ValueError(f"Unsupported schedule history status: {status}")
    if ended_at is None:
        ended_at = time.time()
    cursor = await db.execute(
        """
        UPDATE schedules
        SET status=?, ended_at=?
        WHERE id=? AND status='active'
        """,
        (status, ended_at, schedule_id),
    )
    changed = cursor.rowcount > 0
    if changed:
        await trim_schedule_history(db)
    return changed


async def fetch_schedule_history(db, limit: int = HISTORY_LIMIT) -> list[dict]:
    cursor = await db.execute(
        """
        SELECT *
        FROM schedules
        WHERE status IN ('triggered', 'cancelled')
        ORDER BY ended_at DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, values)) for values in await cursor.fetchall()]
