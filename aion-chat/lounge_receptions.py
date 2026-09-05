"""Read existing local Visitor Lounge conversations without modifying its DB."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from config import BASE_DIR


RECEPTION_PREFIX = "reception:"
LOUNGE_DATABASE_PATH = BASE_DIR.parent / "AionsHome-Visitor-Lounge/data/visitor-lounge.sqlite3"

# Lounge messages belong to a visitor rather than a visit. Bound each conversation
# by its start/end and the next visit, so repeat visits never mix their messages.
# Identity claiming saves the first welcome before the first visit is opened.
_MESSAGES_IN_VISIT = """
    m.visitor_id = v.visitor_id AND m.delivery_status = 'accepted'
    AND (julianday(m.created_at) >= julianday(v.started_at) OR NOT EXISTS (
        SELECT 1 FROM visits previous_visit
        WHERE previous_visit.visitor_id = v.visitor_id
          AND julianday(previous_visit.started_at) < julianday(v.started_at)
    ))
    AND (v.ended_at IS NULL OR julianday(m.created_at) <= julianday(v.ended_at))
    AND NOT EXISTS (
        SELECT 1 FROM visits next_visit
        WHERE next_visit.visitor_id = v.visitor_id
          AND julianday(next_visit.started_at) > julianday(v.started_at)
          AND julianday(next_visit.started_at) <= julianday(m.created_at)
    )
"""
_VISIT_SELECT = f"""
    SELECT v.id, v.started_at, v.ended_at, p.display_name,
           (SELECT COUNT(*) FROM messages m
            WHERE {_MESSAGES_IN_VISIT} AND m.sender = 'host') AS turn_count
    FROM visits v JOIN visitors p ON p.id = v.visitor_id
"""


def _timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _visit(row):
    return {
        "id": RECEPTION_PREFIX + row[0],
        "actor_id": "connor",
        "direction": "inbound",
        "partner_name": row[3] or "访客",
        "friend_id": "",
        "topic": "来家里做客",
        "status": "running" if row[2] is None else "ended",
        "turn_count": row[4],
        "started_at": _timestamp(row[1]),
        "finished_at": _timestamp(row[2]),
    }


class LoungeReceptionHistory:
    def __init__(self, path: Path = LOUNGE_DATABASE_PATH):
        self.path = path

    def _connect(self):
        # mode=ro also prevents accidentally creating a blank lounge database.
        return aiosqlite.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    async def recent(self, actor_id: str, limit: int = 50) -> list[dict]:
        if actor_id != "connor" or not self.path.is_file():
            return []
        async with self._connect() as db:
            cursor = await db.execute(
                _VISIT_SELECT + " ORDER BY julianday(v.started_at) DESC, v.id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            )
            return [_visit(row) for row in await cursor.fetchall()]

    async def get(self, actor_id: str, visit_id: str) -> dict | None:
        if actor_id != "connor" or not visit_id.startswith(RECEPTION_PREFIX) or not self.path.is_file():
            return None
        local_id = visit_id[len(RECEPTION_PREFIX):]
        async with self._connect() as db:
            cursor = await db.execute(_VISIT_SELECT + " WHERE v.id = ?", (local_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            visit = _visit(row)
            cursor = await db.execute(
                f"""SELECT m.id, m.sender, m.content, m.created_at
                    FROM visits v JOIN messages m ON {_MESSAGES_IN_VISIT}
                    WHERE v.id = ? ORDER BY m.rowid ASC""",
                (local_id,),
            )
            visit["messages"] = [
                {
                    "id": message[0],
                    "direction": "outbound" if message[1] == "host" else "inbound",
                    "content": message[2],
                    "created_at": _timestamp(message[3]),
                }
                for message in await cursor.fetchall()
            ]
            return visit
