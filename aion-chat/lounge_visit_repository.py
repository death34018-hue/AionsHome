"""SQLite persistence for local lounge visit history."""

from __future__ import annotations

import re
import time
import uuid
from typing import Callable


_MAX_ERROR_LENGTH = 300
_AUTHORIZATION_RE = re.compile(
    r"""(?ix)
    \bauthorization(?:\s+header)?\b[\"']?\s*[:=]\s*[\"']?
    [^,;}&\]\r\n\"']+(?:\s+[^,;}&\]\r\n\"']+)*
    """
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SECRET_FIELD_RE = re.compile(
    r"""(?ix)
    (?:
        [\"']?(?:visitor[_\s-]?key|oauth(?:[_\s-]?(?:access|refresh|id)?[_\s-]?token)?|
        access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|token)[\"']?\s*[:=]\s*
        | \bvisitor[_\s-]?key\b\s+
    )
    (?:[\"']?)[^,\s;}&\]\r\n\"']+
    """
)


async def ensure_lounge_visit_tables(db) -> None:
    """Create the visit-history schema used by the local coordinator."""
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS lounge_visits (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            friend_id TEXT NOT NULL,
            trigger_source TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            turn_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL
        );

        CREATE TABLE IF NOT EXISTS lounge_visit_messages (
            id TEXT PRIMARY KEY,
            visit_id TEXT NOT NULL REFERENCES lounge_visits(id) ON DELETE CASCADE,
            direction TEXT NOT NULL CHECK(direction IN ('outbound', 'inbound')),
            content TEXT NOT NULL,
            remote_message_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lounge_visits_actor_friend_started
            ON lounge_visits(actor_id, friend_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_lounge_visit_messages_visit_created
            ON lounge_visit_messages(visit_id, created_at ASC);
        """
    )
    await db.commit()


def sanitize_error(error: object) -> str:
    """Persist only a bounded error category and redacted, single-line message."""
    if not error:
        return ""
    if isinstance(error, BaseException):
        category = type(error).__name__
        message = str(error)
    elif isinstance(error, str):
        category = "Error"
        message = error
    else:
        category = type(error).__name__
        message = "An unexpected error occurred."

    message = _redact_sensitive_text(message)
    prefix = f"{category[:80]}: "
    return f"{prefix}{message[: max(0, _MAX_ERROR_LENGTH - len(prefix))]}"


def _sanitize_persisted_text(value: object) -> str:
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: An unexpected error occurred."
    if not isinstance(value, str):
        return ""
    return _redact_sensitive_text(value)


def _redact_sensitive_text(value: str) -> str:
    text = " ".join(value.split())
    text = _AUTHORIZATION_RE.sub("[redacted]", text)
    text = _BEARER_RE.sub("[redacted]", text)
    return _SECRET_FIELD_RE.sub("[redacted]", text)


class LoungeVisitRepository:
    def __init__(self, db, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock

    async def start(
        self, actor_id: str, friend_id: str, trigger_source: str, topic: str
    ) -> str:
        visit_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO lounge_visits "
            "(id,actor_id,friend_id,trigger_source,topic,status,started_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                visit_id,
                actor_id,
                friend_id,
                trigger_source,
                _sanitize_persisted_text(topic),
                "running",
                float(self.clock()),
            ),
        )
        await self.db.commit()
        return visit_id

    async def append_message(
        self,
        visit_id: str,
        direction: str,
        content: str,
        remote_message_id: str = "",
    ) -> str:
        if direction not in {"outbound", "inbound"}:
            raise ValueError("Invalid lounge visit message direction")
        message_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO lounge_visit_messages "
            "(id,visit_id,direction,content,remote_message_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                message_id,
                visit_id,
                direction,
                _sanitize_persisted_text(content),
                _sanitize_persisted_text(remote_message_id),
                float(self.clock()),
            ),
        )
        await self.db.commit()
        return message_id

    async def finish(
        self, visit_id: str, status: str, turn_count: int, error: object = ""
    ) -> None:
        await self.db.execute(
            "UPDATE lounge_visits SET status=?,turn_count=?,error=?,finished_at=? "
            "WHERE id=?",
            (
                status,
                turn_count,
                sanitize_error(error),
                float(self.clock()),
                visit_id,
            ),
        )
        await self.db.commit()

    async def finish_running(
        self, actor_id: str, visit_id: str, reason: str
    ) -> bool:
        cursor = await self.db.execute(
            "UPDATE lounge_visits SET status='interrupted',error=?,finished_at=? "
            "WHERE id=? AND actor_id=? AND status='running'",
            (
                sanitize_error(reason),
                float(self.clock()),
                visit_id,
                actor_id,
            ),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def interrupt_stale_running(self, reason: str) -> int:
        cursor = await self.db.execute(
            "UPDATE lounge_visits SET status='interrupted',error=?,finished_at=? "
            "WHERE status='running'",
            (sanitize_error(reason), float(self.clock())),
        )
        await self.db.commit()
        return cursor.rowcount

    async def update_progress(self, visit_id: str, turn_count: int) -> None:
        await self.db.execute(
            "UPDATE lounge_visits SET turn_count=? WHERE id=? AND status='running'",
            (max(0, int(turn_count)), visit_id),
        )
        await self.db.commit()

    async def delete(self, actor_id: str, visit_id: str) -> bool:
        """Delete one actor-owned finished visit and its local messages."""
        await self.db.execute(
            "DELETE FROM lounge_visit_messages WHERE visit_id IN ("
            "SELECT id FROM lounge_visits WHERE id=? AND actor_id=? AND status!='running'"
            ")",
            (visit_id, actor_id),
        )
        cursor = await self.db.execute(
            "DELETE FROM lounge_visits WHERE id=? AND actor_id=? AND status!='running'",
            (visit_id, actor_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get(self, actor_id: str, visit_id: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id,actor_id,friend_id,trigger_source,topic,status,turn_count,error,"
            "started_at,finished_at FROM lounge_visits WHERE id=? AND actor_id=?",
            (visit_id, actor_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        visit = self._visit_dict(row)
        cursor = await self.db.execute(
            "SELECT id,direction,content,remote_message_id,created_at "
            "FROM lounge_visit_messages WHERE visit_id=? ORDER BY created_at ASC, id ASC",
            (visit_id,),
        )
        visit["messages"] = [self._message_dict(message) for message in await cursor.fetchall()]
        return visit

    async def recent(
        self, actor_id: str, friend_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        bounded_limit = max(1, min(int(limit), 100))
        if friend_id is None:
            cursor = await self.db.execute(
                "SELECT id,actor_id,friend_id,trigger_source,topic,status,turn_count,error,"
                "started_at,finished_at FROM lounge_visits WHERE actor_id=? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (actor_id, bounded_limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT id,actor_id,friend_id,trigger_source,topic,status,turn_count,error,"
                "started_at,finished_at FROM lounge_visits WHERE actor_id=? AND friend_id=? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (actor_id, friend_id, bounded_limit),
            )
        return [self._visit_dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _visit_dict(row) -> dict:
        return {
            "id": row[0],
            "actor_id": row[1],
            "friend_id": row[2],
            "trigger_source": row[3],
            "topic": row[4],
            "status": row[5],
            "turn_count": row[6],
            "error": row[7],
            "started_at": row[8],
            "finished_at": row[9],
        }

    @staticmethod
    def _message_dict(row) -> dict:
        return {
            "id": row[0],
            "direction": row[1],
            "content": row[2],
            "remote_message_id": row[3],
            "created_at": row[4],
        }
