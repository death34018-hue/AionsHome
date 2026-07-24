"""Incremental replay endpoint for cross-device state convergence."""

import json

import aiosqlite
from fastapi import APIRouter, Query

from database import get_db


router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/changes")
async def sync_changes(
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COALESCE(MIN(seq), 0), COALESCE(MAX(seq), 0) FROM sync_events")
        min_seq, latest_seq = await cur.fetchone()
        min_seq = int(min_seq or 0)
        latest_seq = int(latest_seq or 0)
        reset_required = bool(
            (after and min_seq and after < min_seq - 1)
            or after > latest_seq
        )
        if reset_required:
            return {
                "events": [],
                "latest_seq": latest_seq,
                "has_more": False,
                "reset_required": True,
            }

        cur = await db.execute(
            "SELECT seq, event_type, payload, created_at FROM sync_events "
            "WHERE seq>? ORDER BY seq ASC LIMIT ?",
            (after, limit + 1),
        )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    events = []
    for row in rows[:limit]:
        try:
            data = json.loads(row["payload"] or "null")
        except Exception:
            data = None
        events.append({
            "type": row["event_type"],
            "data": data,
            "sync_seq": int(row["seq"]),
            "created_at": row["created_at"],
        })
    return {
        "events": events,
        "latest_seq": latest_seq,
        "has_more": has_more,
        "reset_required": False,
    }
