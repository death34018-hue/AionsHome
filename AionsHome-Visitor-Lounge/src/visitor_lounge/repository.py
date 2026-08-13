"""Ownership-scoped persistence operations for visitor identities."""

from datetime import datetime, timedelta
import hmac
import json
import sqlite3
from uuid import uuid4

from visitor_lounge.database import Database, utc_now
from visitor_lounge.models import (
    DeliveryStatus,
    GenerationJobRecord,
    Message,
    MessageSource,
    RuntimeResourceState,
    Summary,
    SummaryJob,
    VisitorKind,
    VisitorRecord,
    VisitorSession,
)


class VisitorNotFound(ValueError):
    """Raised when an operation names a visitor that does not exist."""


class VisitorAlreadyClaimed(ValueError):
    """Raised when a visitor attempts to claim a name more than once."""


class RuntimeStateRepository:
    """Persist lightweight cross-process runtime observations."""

    _RESOURCE_GATE_KEY = "resource_gate"

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_resource_gate(self, *, can_start: bool, checked_at: datetime) -> None:
        with self._database.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (
                    self._RESOURCE_GATE_KEY,
                    "available" if can_start else "paused",
                    _timestamp(checked_at),
                ),
            )

    def resource_gate(self) -> RuntimeResourceState | None:
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                (self._RESOURCE_GATE_KEY,),
            ).fetchone()
        if row is None:
            return None
        return RuntimeResourceState(
            can_start=str(row[0]) == "available",
            checked_at=datetime.fromisoformat(str(row[1])),
        )


def _timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def _message_from_row(row: sqlite3.Row | tuple[object, ...]) -> Message:
    return Message(
        id=str(row[0]),
        visitor_id=str(row[1]),
        sender=str(row[2]),  # type: ignore[arg-type]
        content=str(row[3]),
        created_at=datetime.fromisoformat(str(row[4])),
        source=str(row[5]),  # type: ignore[arg-type]
        delivery_status=str(row[6]),  # type: ignore[arg-type]
    )


def _visitor_from_row(row: sqlite3.Row | tuple[object, ...]) -> VisitorRecord:
    return VisitorRecord(
        id=str(row[0]),
        display_name=None if row[1] is None else str(row[1]),
        status=str(row[2]),
        disclosure_version=None if row[3] is None else str(row[3]),
        visitor_kind=str(row[4]),  # type: ignore[arg-type]
        safety_locked_until=(
            None if row[5] is None else datetime.fromisoformat(str(row[5]))
        ),
    )


def _insert_message(
    conn: sqlite3.Connection,
    visitor_id: str,
    sender: str,
    content: str,
    created_at: datetime,
    *,
    source: MessageSource = "web",
    delivery_status: DeliveryStatus = "accepted",
) -> Message:
    message = Message(
        id=str(uuid4()),
        visitor_id=visitor_id,
        sender=sender,  # type: ignore[arg-type]
        content=content,
        created_at=created_at,
        source=source,
        delivery_status=delivery_status,
    )
    conn.execute(
        """
        INSERT INTO messages
            (id, visitor_id, sender, content, created_at, source, delivery_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.id,
            message.visitor_id,
            message.sender,
            message.content,
            _timestamp(message.created_at),
            message.source,
            message.delivery_status,
        ),
    )
    return message


def _summary_job_from_row(row: sqlite3.Row | tuple[object, ...]) -> SummaryJob:
    return SummaryJob(
        id=str(row[0]),
        visitor_id=str(row[1]),
        first_message_id=str(row[2]),
        last_message_id=str(row[3]),
    )


class MessageRepository:
    """Persist messages while requiring an explicit visitor owner."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def append(
        self,
        visitor_id: str,
        sender: str,
        content: str,
        *,
        created_at: datetime | None = None,
        source: MessageSource = "web",
        delivery_status: DeliveryStatus = "accepted",
    ) -> Message:
        timestamp = created_at or utc_now()
        with self._database.transaction(immediate=True) as conn:
            try:
                return _insert_message(
                    conn,
                    visitor_id,
                    sender,
                    content,
                    timestamp,
                    source=source,
                    delivery_status=delivery_status,
                )
            except sqlite3.IntegrityError as exc:
                exists = conn.execute(
                    "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
                ).fetchone()
                if exists is None:
                    raise VisitorNotFound(visitor_id) from exc
                raise

    def append_message(
        self,
        visitor_id: str,
        sender: str,
        content: str,
        *,
        created_at: datetime | None = None,
        source: MessageSource = "web",
        delivery_status: DeliveryStatus = "accepted",
    ) -> Message:
        return self.append(
            visitor_id,
            sender,
            content,
            created_at=created_at,
            source=source,
            delivery_status=delivery_status,
        )

    def recent(self, visitor_id: str, limit: int = 10) -> list[Message]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        if limit == 0:
            return []
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, visitor_id, sender, content, created_at,
                       source, delivery_status
                FROM messages
                WHERE visitor_id = ? AND delivery_status = 'accepted'
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (visitor_id, limit),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def recent_messages(self, visitor_id: str, limit: int = 10) -> list[Message]:
        return self.recent(visitor_id, limit)

    def timeline(
        self,
        visitor_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 30,
    ) -> list[Message]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        if limit == 0:
            return []
        with self._database.connection() as conn:
            cursor = None
            if after_message_id is not None:
                cursor = conn.execute(
                    "SELECT rowid FROM messages WHERE id = ? AND visitor_id = ?",
                    (after_message_id, visitor_id),
                ).fetchone()
            if cursor is not None:
                rows = conn.execute(
                    """
                    SELECT id, visitor_id, sender, content, created_at,
                           source, delivery_status
                    FROM messages
                    WHERE visitor_id = ? AND rowid > ?
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (visitor_id, int(cursor[0]), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, visitor_id, sender, content, created_at,
                           source, delivery_status
                    FROM messages
                    WHERE visitor_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (visitor_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
        return [_message_from_row(row) for row in rows]

    def mark_failed(self, message_id: str) -> bool:
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE messages SET delivery_status = 'failed'
                WHERE id = ? AND sender = 'visitor'
                """,
                (message_id,),
            )
            return updated.rowcount == 1


class VisitorRepository:
    """Database access with explicit visitor ownership at every data boundary."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create_unclaimed_visitor(
        self,
        visitor_kind: VisitorKind = "human",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        if visitor_kind not in {"human", "external_ai"}:
            raise ValueError("unsupported visitor kind")
        visitor_id = str(uuid4())

        def create(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO visitors (id, created_at, visitor_kind)
                VALUES (?, ?, ?)
                """,
                (visitor_id, _timestamp(), visitor_kind),
            )
        if connection is not None:
            create(connection)
            return visitor_id
        with self._database.transaction(immediate=True) as conn:
            create(conn)
        return visitor_id

    def claim_name(
        self,
        visitor_id: str,
        display_name: str,
        disclosure_version: str | None = None,
        *,
        greeting: str | None = None,
        greeting_at: datetime | None = None,
        source: MessageSource = "web",
    ) -> str:
        with self._database.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE visitors
                SET display_name = ?,
                    disclosure_version = ?,
                    disclosure_consented_at = CASE
                        WHEN ? IS NULL THEN disclosure_consented_at
                        ELSE ?
                    END
                WHERE id = ? AND display_name IS NULL
                """,
                (
                    display_name,
                    disclosure_version,
                    disclosure_version,
                    _timestamp(),
                    visitor_id,
                ),
            )
            if cursor.rowcount == 1:
                if greeting:
                    _insert_message(
                        conn,
                        visitor_id,
                        "host",
                        greeting,
                        greeting_at or utc_now(),
                        source=source,
                    )
                return display_name
            exists = conn.execute(
                "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if exists is None:
                raise VisitorNotFound(visitor_id)
            raise VisitorAlreadyClaimed(visitor_id)

    def add_key(
        self,
        visitor_id: str,
        key_hash: str,
        encrypted_value: bytes,
        masked: str,
        *,
        revoke_existing: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def store(conn: sqlite3.Connection) -> None:
            if revoke_existing:
                conn.execute(
                    """
                    UPDATE visitor_keys
                    SET revoked_at = ?
                    WHERE visitor_id = ? AND revoked_at IS NULL
                    """,
                    (_timestamp(), visitor_id),
                )
            try:
                conn.execute(
                    """
                    INSERT INTO visitor_keys
                        (id, visitor_id, key_hash, encrypted_value, masked, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        visitor_id,
                        key_hash,
                        encrypted_value,
                        masked,
                        _timestamp(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VisitorNotFound(visitor_id) from exc

        if connection is not None:
            store(connection)
            return
        with self._database.transaction(immediate=True) as conn:
            store(conn)

    def active_key_candidate(self, key_hash: str) -> tuple[str, str] | None:
        """Return only the credential candidate selected by a keyed digest."""
        identity = self.active_key_identity_candidate(key_hash)
        return None if identity is None else (identity[1], identity[2])

    def active_key_identity_candidate(
        self, key_hash: str
    ) -> tuple[str, str, str] | None:
        """Return Key row ID, visitor ID, and digest for one active credential."""
        with self._database.connection() as conn:
            row = conn.execute(
                """
                SELECT visitor_keys.id, visitor_keys.visitor_id,
                       visitor_keys.key_hash
                FROM visitor_keys
                JOIN visitors ON visitors.id = visitor_keys.visitor_id
                WHERE visitor_keys.key_hash = ?
                  AND visitor_keys.revoked_at IS NULL
                """,
                (key_hash,),
            ).fetchone()
        return (
            None
            if row is None
            else (str(row[0]), str(row[1]), str(row[2]))
        )

    def visitor(self, visitor_id: str) -> VisitorRecord:
        with self._database.connection() as conn:
            row = conn.execute(
                """
                SELECT id, display_name, status, disclosure_version,
                       visitor_kind, safety_locked_until
                FROM visitors WHERE id = ?
                """,
                (visitor_id,),
            ).fetchone()
        if row is None:
            raise VisitorNotFound(visitor_id)
        return _visitor_from_row(row)

    def update_identity(
        self,
        visitor_id: str,
        display_name: str,
        visitor_kind: VisitorKind,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> VisitorRecord:
        if visitor_kind not in {"human", "external_ai"}:
            raise ValueError("unsupported visitor kind")

        def update(conn: sqlite3.Connection) -> VisitorRecord:
            updated = conn.execute(
                """
                UPDATE visitors
                SET display_name = ?, visitor_kind = ?
                WHERE id = ?
                """,
                (display_name, visitor_kind, visitor_id),
            )
            if updated.rowcount != 1:
                raise VisitorNotFound(visitor_id)
            row = conn.execute(
                """
                SELECT id, display_name, status, disclosure_version,
                       visitor_kind, safety_locked_until
                FROM visitors WHERE id = ?
                """,
                (visitor_id,),
            ).fetchone()
            if row is None:
                raise VisitorNotFound(visitor_id)
            return _visitor_from_row(row)

        if connection is not None:
            return update(connection)
        with self._database.transaction(immediate=True) as conn:
            return update(conn)

    def release_expired_safety_lock(
        self,
        visitor_id: str,
        now: datetime,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        def release(conn: sqlite3.Connection) -> bool:
            updated = conn.execute(
                """
                UPDATE visitors
                SET status = 'active', safety_locked_until = NULL
                WHERE id = ?
                  AND status = 'safety_lock'
                  AND safety_locked_until IS NOT NULL
                  AND julianday(safety_locked_until) <= julianday(?)
                """,
                (visitor_id, _timestamp(now)),
            )
            return updated.rowcount == 1

        if connection is not None:
            return release(connection)
        with self._database.transaction(immediate=True) as conn:
            return release(conn)

    def end_visit(
        self,
        visitor_id: str,
        ended_at: datetime,
        status: str = "suspended",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if status not in {"suspended", "safety_lock"}:
            raise ValueError("unsupported end status")

        def end(conn: sqlite3.Connection) -> None:
            exists = conn.execute(
                "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if exists is None:
                raise VisitorNotFound(visitor_id)
            conn.execute(
                """
                UPDATE visits SET ended_at = ?
                WHERE visitor_id = ? AND ended_at IS NULL
                """,
                (_timestamp(ended_at), visitor_id),
            )
            if status == "safety_lock":
                conn.execute(
                    "UPDATE visitors SET status = 'safety_lock' WHERE id = ?",
                    (visitor_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE visitors
                    SET status = 'suspended', safety_locked_until = NULL
                    WHERE id = ?
                    """,
                    (visitor_id,),
                )

        if connection is not None:
            end(connection)
            return
        with self._database.transaction(immediate=True) as conn:
            end(conn)

    def set_status(
        self,
        visitor_id: str,
        status: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if status not in {"active", "paused", "suspended", "safety_lock"}:
            raise ValueError("unsupported visitor status")

        def update(conn: sqlite3.Connection) -> None:
            updated = conn.execute(
                """
                UPDATE visitors
                SET status = ?,
                    safety_locked_until = CASE
                        WHEN ? = 'safety_lock' THEN safety_locked_until
                        ELSE NULL
                    END
                WHERE id = ?
                """,
                (status, status, visitor_id),
            )
            if updated.rowcount != 1:
                raise VisitorNotFound(visitor_id)

        if connection is not None:
            update(connection)
            return
        with self._database.transaction(immediate=True) as conn:
            update(conn)

    def job(self, request_id: str) -> GenerationJobRecord:
        return self._job("generation_jobs.request_id = ?", request_id)

    def job_by_id(self, job_id: str) -> GenerationJobRecord:
        return self._job("generation_jobs.id = ?", job_id)

    def latest_job(self, visitor_id: str) -> GenerationJobRecord | None:
        try:
            return self._job(
                "generation_jobs.visitor_id = ? ORDER BY generation_jobs.created_at DESC, generation_jobs.id DESC LIMIT 1",
                visitor_id,
            )
        except KeyError:
            return None

    def _job(self, predicate: str, value: str) -> GenerationJobRecord:
        with self._database.connection() as conn:
            row = conn.execute(
                f"""
                SELECT generation_jobs.id, generation_jobs.request_id,
                       generation_jobs.visitor_id, generation_jobs.message_id,
                       generation_jobs.response_message_id,
                       generation_jobs.status, generation_jobs.visible_text,
                       generation_jobs.action,
                       COALESCE(model_calls.usage_reported, 0),
                       COALESCE(model_calls.input_tokens, 0),
                       COALESCE(model_calls.output_tokens, 0),
                       generation_jobs.created_at, generation_jobs.finished_at
                FROM generation_jobs
                LEFT JOIN model_calls ON model_calls.job_id = generation_jobs.id
                WHERE {predicate}
                """,
                (value,),
            ).fetchone()
        if row is None:
            raise KeyError(value)
        return GenerationJobRecord(
            id=str(row[0]),
            request_id=str(row[1]),
            visitor_id=str(row[2]),
            message_id=None if row[3] is None else str(row[3]),
            response_message_id=None if row[4] is None else str(row[4]),
            status=str(row[5]),
            visible_text=str(row[6]),
            action=None if row[7] is None else str(row[7]),
            usage_reported=bool(row[8]),
            input_tokens=int(row[9]),
            output_tokens=int(row[10]),
            created_at=datetime.fromisoformat(str(row[11])),
            finished_at=(
                None if row[12] is None else datetime.fromisoformat(str(row[12]))
            ),
        )

    def encrypted_keys_for_visitor(self, visitor_id: str) -> list[tuple[bytes, str]]:
        """Admin-facing key copies are always selected for an explicit owner."""
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT encrypted_value, masked
                FROM visitor_keys
                WHERE visitor_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (visitor_id,),
            ).fetchall()
        return [(bytes(row[0]), str(row[1])) for row in rows]

    def replace_sessions(
        self,
        visitor_id: str,
        device_id: str,
        session_hash: str,
        expires_at: datetime,
    ) -> VisitorSession:
        """Revoke a visitor's active sessions and create one new session atomically."""
        session_id = str(uuid4())
        created_at = _timestamp()
        with self._database.transaction(immediate=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if exists is None:
                raise VisitorNotFound(visitor_id)
            conn.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE visitor_id = ? AND revoked_at IS NULL
                """,
                (created_at, visitor_id),
            )
            conn.execute(
                """
                INSERT INTO auth_sessions
                    (id, visitor_id, device_id, session_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, visitor_id, device_id, session_hash, created_at, _timestamp(expires_at)),
            )
        return VisitorSession(session_id, visitor_id, device_id, expires_at)

    def active_session_for_visitor(
        self, visitor_id: str, session_hash: str, now: datetime
    ) -> VisitorSession | None:
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, visitor_id, device_id, session_hash, expires_at
                FROM auth_sessions
                WHERE visitor_id = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (visitor_id, _timestamp(now)),
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(str(row[3]), session_hash):
                return VisitorSession(
                    id=str(row[0]),
                    visitor_id=str(row[1]),
                    device_id=str(row[2]),
                    expires_at=datetime.fromisoformat(str(row[4])),
                )
        return None

    def revoke_session(self, session_id: str) -> None:
        with self._database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (_timestamp(), session_id),
            )


class BackgroundRepository:
    """Persist per-visitor activity and immutable coarse-summary snapshots."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_login(
        self,
        visitor_id: str,
        at: datetime,
        *,
        returning_greeting: str | None = None,
        source: MessageSource = "web",
    ) -> None:
        timestamp = _timestamp(at)
        with self._database.transaction(immediate=True) as conn:
            visitor = conn.execute(
                "SELECT status, display_name FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if visitor is None:
                raise VisitorNotFound(visitor_id)
            status, display_name = str(visitor[0]), visitor[1]
            if status not in {"active", "suspended"}:
                return
            open_visit = conn.execute(
                "SELECT id FROM visits WHERE visitor_id = ? AND ended_at IS NULL LIMIT 1",
                (visitor_id,),
            ).fetchone()
            if status == "suspended":
                conn.execute(
                    "UPDATE visitors SET status = 'active' WHERE id = ? AND status = 'suspended'",
                    (visitor_id,),
                )
            if open_visit is None:
                conn.execute(
                    "INSERT INTO visits (id, visitor_id, started_at, last_activity_at) VALUES (?, ?, ?, ?)",
                    (str(uuid4()), visitor_id, timestamp, timestamp),
                )
                if status == "suspended" and display_name and returning_greeting:
                    _insert_message(
                        conn,
                        visitor_id,
                        "host",
                        returning_greeting.replace("{访客名字}", str(display_name)),
                        at,
                        source=source,
                    )
            else:
                conn.execute(
                    "UPDATE visits SET last_activity_at = ? WHERE id = ?",
                    (timestamp, str(open_visit[0])),
                )

    def record_activity(self, visitor_id: str, at: datetime) -> None:
        timestamp = _timestamp(at)
        with self._database.transaction(immediate=True) as conn:
            visitor = conn.execute(
                "SELECT status FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if visitor is None:
                raise VisitorNotFound(visitor_id)
            status = str(visitor[0])
            if status not in {"active", "suspended"}:
                return
            if status == "suspended":
                conn.execute(
                    "UPDATE visitors SET status = 'active' WHERE id = ? "
                    "AND status = 'suspended'",
                    (visitor_id,),
                )
            open_visit = conn.execute(
                """
                SELECT id, last_activity_at
                FROM visits
                WHERE visitor_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
            if open_visit is None:
                conn.execute(
                    """
                    INSERT INTO visits
                        (id, visitor_id, started_at, last_activity_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid4()), visitor_id, timestamp, timestamp),
                )
            elif datetime.fromisoformat(str(open_visit[1])) < at:
                conn.execute(
                    "UPDATE visits SET last_activity_at = ? WHERE id = ?",
                    (timestamp, str(open_visit[0])),
                )

    def suspend_idle_visits(self, now: datetime, *, idle_minutes: int = 30) -> int:
        cutoff = _timestamp(now - timedelta(minutes=idle_minutes))
        with self._database.transaction(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT visitors.id, visits.id
                FROM visitors
                JOIN visits ON visits.visitor_id = visitors.id
                WHERE visitors.status = 'active'
                  AND visits.ended_at IS NULL
                  AND visits.last_activity_at <= ?
                ORDER BY visitors.id
                """,
                (cutoff,),
            ).fetchall()
            suspended = 0
            for visitor_id, visit_id in rows:
                updated = conn.execute(
                    """
                    UPDATE visitors SET status = 'suspended'
                    WHERE id = ? AND status = 'active'
                    """,
                    (str(visitor_id),),
                )
                if updated.rowcount != 1:
                    continue
                conn.execute(
                    "UPDATE visits SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (_timestamp(now), str(visit_id)),
                )
                suspended += 1
        return suspended

    def summary_candidates(
        self,
        now: datetime,
        *,
        idle_minutes: int = 20,
        minimum_new: int = 15,
    ) -> list[str]:
        cutoff = now - timedelta(minutes=idle_minutes)
        with self._database.connection() as conn:
            visitors = conn.execute(
                """
                SELECT id, last_summarized_message_id
                FROM visitors
                WHERE NOT EXISTS (
                    SELECT 1 FROM summary_jobs
                    WHERE summary_jobs.visitor_id = visitors.id
                      AND summary_jobs.status IN ('queued', 'running', 'failed')
                )
                ORDER BY id
                """
            ).fetchall()
            candidates: list[str] = []
            for visitor_id, cursor_id in visitors:
                cursor_rowid = 0
                if cursor_id is not None:
                    cursor = conn.execute(
                        """
                        SELECT rowid FROM messages
                        WHERE id = ? AND visitor_id = ?
                          AND delivery_status = 'accepted'
                        """,
                        (str(cursor_id), str(visitor_id)),
                    ).fetchone()
                    if cursor is not None:
                        cursor_rowid = int(cursor[0])
                rows = conn.execute(
                    """
                    SELECT created_at
                    FROM messages
                    WHERE visitor_id = ? AND sender = 'visitor' AND rowid > ?
                      AND delivery_status = 'accepted'
                    ORDER BY rowid
                    """,
                    (str(visitor_id), cursor_rowid),
                ).fetchall()
                if len(rows) < minimum_new:
                    continue
                if datetime.fromisoformat(str(rows[-1][0])) > cutoff:
                    continue
                candidates.append(str(visitor_id))
        return candidates

    def next_unsummarized_visitor_messages(
        self, visitor_id: str, *, limit: int = 15
    ) -> list[Message]:
        with self._database.connection() as conn:
            visitor = conn.execute(
                "SELECT last_summarized_message_id FROM visitors WHERE id = ?",
                (visitor_id,),
            ).fetchone()
            if visitor is None:
                raise VisitorNotFound(visitor_id)
            cursor_rowid = 0
            if visitor[0] is not None:
                cursor = conn.execute(
                    """
                    SELECT rowid FROM messages
                    WHERE id = ? AND visitor_id = ?
                      AND delivery_status = 'accepted'
                    """,
                    (str(visitor[0]), visitor_id),
                ).fetchone()
                if cursor is not None:
                    cursor_rowid = int(cursor[0])
            rows = conn.execute(
                """
                SELECT id, visitor_id, sender, content, created_at,
                       source, delivery_status
                FROM messages
                WHERE visitor_id = ? AND sender = 'visitor' AND rowid > ?
                  AND delivery_status = 'accepted'
                ORDER BY rowid
                LIMIT ?
                """,
                (visitor_id, cursor_rowid, limit),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def summary_context(
        self, visitor_id: str, first_message_id: str, last_message_id: str
    ) -> list[Message]:
        with self._database.connection() as conn:
            bounds = conn.execute(
                """
                SELECT
                    (SELECT rowid FROM messages
                     WHERE id = ? AND visitor_id = ?
                       AND delivery_status = 'accepted'),
                    (SELECT rowid FROM messages
                     WHERE id = ? AND visitor_id = ?
                       AND delivery_status = 'accepted')
                """,
                (
                    first_message_id,
                    visitor_id,
                    last_message_id,
                    visitor_id,
                ),
            ).fetchone()
            if bounds is None or bounds[0] is None or bounds[1] is None:
                raise KeyError((first_message_id, last_message_id))
            first_rowid = int(bounds[0])
            last_visitor_rowid = int(bounds[1])
            next_visitor = conn.execute(
                """
                SELECT rowid FROM messages
                WHERE visitor_id = ? AND sender = 'visitor' AND rowid > ?
                  AND delivery_status = 'accepted'
                ORDER BY rowid LIMIT 1
                """,
                (visitor_id, last_visitor_rowid),
            ).fetchone()
            if next_visitor is None:
                final_row = conn.execute(
                    """
                    SELECT MAX(rowid) FROM messages
                    WHERE visitor_id = ? AND delivery_status = 'accepted'
                    """,
                    (visitor_id,),
                ).fetchone()
                end_rowid = (
                    int(final_row[0])
                    if final_row and final_row[0]
                    else last_visitor_rowid
                )
            else:
                end_rowid = int(next_visitor[0]) - 1
            rows = conn.execute(
                """
                SELECT id, visitor_id, sender, content, created_at,
                       source, delivery_status
                FROM messages
                WHERE visitor_id = ? AND rowid BETWEEN ? AND ?
                  AND delivery_status = 'accepted'
                ORDER BY rowid
                """,
                (visitor_id, first_rowid, end_rowid),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def create_summary_job(
        self,
        visitor_id: str,
        first_message_id: str,
        last_message_id: str,
        now: datetime,
    ) -> SummaryJob:
        job = SummaryJob(str(uuid4()), visitor_id, first_message_id, last_message_id)
        with self._database.transaction(immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT id, visitor_id, first_message_id, last_message_id
                FROM summary_jobs
                WHERE visitor_id = ? AND status IN ('queued', 'running', 'failed')
                """,
                (visitor_id,),
            ).fetchone()
            if existing is not None:
                return _summary_job_from_row(existing)
            conn.execute(
                """
                INSERT INTO summary_jobs
                    (id, visitor_id, first_message_id, last_message_id,
                     status, created_at)
                VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (
                    job.id,
                    visitor_id,
                    first_message_id,
                    last_message_id,
                    _timestamp(now),
                ),
            )
        return job

    def due_summary_jobs(self, now: datetime) -> list[SummaryJob]:
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, visitor_id, first_message_id, last_message_id
                FROM summary_jobs
                WHERE status = 'queued'
                   OR (status = 'failed' AND next_retry_at IS NOT NULL
                                         AND next_retry_at <= ?)
                ORDER BY created_at, id
                """,
                (_timestamp(now),),
            ).fetchall()
        return [_summary_job_from_row(row) for row in rows]

    def recover_abandoned_summary_jobs(self, now: datetime) -> int:
        """Move summaries left running by a prior process into persisted retry."""
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, visitor_id, first_message_id, last_message_id
                FROM summary_jobs
                WHERE status = 'running'
                ORDER BY created_at, id
                """
            ).fetchall()
        jobs = [_summary_job_from_row(row) for row in rows]
        for job in jobs:
            self.fail_summary(job, now)
        return len(jobs)

    def recover_abandoned_summary_attempts(self, now: datetime) -> int:
        """Close model calls left running by a prior coordinator process."""
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE summary_generation_attempts
                SET status = 'interrupted', finished_at = ?,
                    failure_reason = 'coordinator_restart'
                WHERE status = 'running'
                """,
                (_timestamp(now),),
            )
        return updated.rowcount

    def interrupt_running_summary_attempts(self, now: datetime) -> int:
        """Close this coordinator's running calls without ending their tasks."""
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE summary_generation_attempts
                SET status = 'interrupted', finished_at = ?,
                    failure_reason = 'coordinator_stop'
                WHERE status = 'running'
                """,
                (_timestamp(now),),
            )
        return updated.rowcount

    def start_summary_attempt(
        self, job_id: str, visitor_id: str, now: datetime
    ) -> str:
        """Atomically acquire the job run and record one actual generator entry."""
        attempt_id = str(uuid4())
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'running', started_at = ?, next_retry_at = NULL
                WHERE id = ? AND visitor_id = ?
                  AND status IN ('queued', 'failed')
                """,
                (_timestamp(now), job_id, visitor_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("summary job is not available for generation")
            conn.execute(
                """
                INSERT INTO summary_generation_attempts
                    (id, summary_job_id, visitor_id, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (attempt_id, job_id, visitor_id, _timestamp(now)),
            )
        return attempt_id

    def finish_summary_attempt(
        self,
        attempt_id: str,
        status: str,
        now: datetime,
        *,
        usage: dict[str, int] | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "timed_out", "interrupted"}:
            raise ValueError("invalid summary attempt terminal status")
        reported = _usage_is_reported(usage)
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE summary_generation_attempts
                SET status = ?, usage_reported = ?, input_tokens = ?,
                    output_tokens = ?, failure_reason = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    int(reported),
                    _safe_usage_value(usage or {}, "input_tokens") if reported else 0,
                    _safe_usage_value(usage or {}, "output_tokens") if reported else 0,
                    failure_reason,
                    _timestamp(now),
                    attempt_id,
                ),
            )
            # Restart recovery may have closed the row while an old cancelled
            # task was still unwinding; terminal closure is intentionally idempotent.

    def supplement_summary_attempt_usage(
        self, attempt_id: str, usage: dict[str, int]
    ) -> bool:
        """Attach late model-reported usage without changing terminal outcome."""
        if not _usage_is_reported(usage):
            return False
        with self._database.transaction(immediate=True) as conn:
            updated = conn.execute(
                """
                UPDATE summary_generation_attempts
                SET usage_reported = 1, input_tokens = ?, output_tokens = ?
                WHERE id = ? AND status IN ('timed_out', 'interrupted')
                  AND usage_reported = 0
                """,
                (
                    _safe_usage_value(usage, "input_tokens"),
                    _safe_usage_value(usage, "output_tokens"),
                    attempt_id,
                ),
            )
        return updated.rowcount == 1

    def mark_summary_running(self, job_id: str, now: datetime) -> None:
        with self._database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'running', started_at = ?, next_retry_at = NULL
                WHERE id = ? AND status IN ('queued', 'failed')
                """,
                (_timestamp(now), job_id),
            )

    def last_summarized_message_id(self, visitor_id: str) -> str | None:
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT last_summarized_message_id FROM visitors WHERE id = ?",
                (visitor_id,),
            ).fetchone()
        if row is None:
            raise VisitorNotFound(visitor_id)
        return None if row[0] is None else str(row[0])

    def recent_summaries(self, visitor_id: str, *, limit: int = 1) -> list[Summary]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        if limit == 0:
            return []
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, visitor_id, first_message_id, last_message_id, text
                FROM summaries
                WHERE visitor_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (visitor_id, limit),
            ).fetchall()
        return [
            Summary(*(str(value) for value in row)) for row in reversed(rows)
        ]

    def memory_status(self, visitor_id: str) -> tuple[bool, datetime | None]:
        with self._database.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
            ).fetchone()
            if exists is None:
                raise VisitorNotFound(visitor_id)
            row = conn.execute(
                """
                SELECT created_at FROM summaries
                WHERE visitor_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
        if row is None:
            return False, None
        return True, datetime.fromisoformat(str(row[0]))

    def complete_summary(
        self,
        job: SummaryJob,
        text: str,
        usage: dict[str, int],
        now: datetime,
    ) -> Summary:
        with self._database.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT status FROM summary_jobs
                WHERE id = ? AND visitor_id = ?
                  AND first_message_id = ? AND last_message_id = ?
                """,
                (
                    job.id,
                    job.visitor_id,
                    job.first_message_id,
                    job.last_message_id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(job.id)
            if str(row[0]) == "completed":
                existing = conn.execute(
                    """
                    SELECT id, visitor_id, first_message_id, last_message_id, text
                    FROM summaries
                    WHERE visitor_id = ? AND first_message_id = ? AND last_message_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (job.visitor_id, job.first_message_id, job.last_message_id),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("completed summary job has no summary")
                return Summary(*(str(value) for value in existing))
            existing_memory = conn.execute(
                "SELECT id FROM summaries WHERE visitor_id = ?",
                (job.visitor_id,),
            ).fetchone()
            summary = Summary(
                id=str(uuid4()) if existing_memory is None else str(existing_memory[0]),
                visitor_id=job.visitor_id,
                first_message_id=job.first_message_id,
                last_message_id=job.last_message_id,
                text=text,
            )
            if existing_memory is None:
                conn.execute(
                    """
                    INSERT INTO summaries
                        (id, visitor_id, first_message_id, last_message_id, text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary.id,
                        summary.visitor_id,
                        summary.first_message_id,
                        summary.last_message_id,
                        summary.text,
                        _timestamp(now),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE summaries
                    SET first_message_id = ?, last_message_id = ?, text = ?, created_at = ?
                    WHERE visitor_id = ?
                    """,
                    (
                        summary.first_message_id,
                        summary.last_message_id,
                        summary.text,
                        _timestamp(now),
                        summary.visitor_id,
                    ),
                )
            conn.execute(
                "UPDATE visitors SET last_summarized_message_id = ? WHERE id = ?",
                (summary.last_message_id, summary.visitor_id),
            )
            conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'completed', finished_at = ?, next_retry_at = NULL,
                    input_tokens = ?, output_tokens = ?
                WHERE id = ?
                """,
                (
                    _timestamp(now),
                    _safe_usage_value(usage, "input_tokens"),
                    _safe_usage_value(usage, "output_tokens"),
                    job.id,
                ),
            )
            conn.execute(
                """
                INSERT INTO notification_events
                    (id, visitor_id, kind, payload, created_at)
                VALUES (?, ?, 'summary_ready', ?, ?)
                """,
                (
                    str(uuid4()),
                    summary.visitor_id,
                    json.dumps(
                        {"summary_id": summary.id},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _timestamp(now),
                ),
            )
        return summary

    def fail_summary(
        self,
        job: SummaryJob,
        now: datetime,
        *,
        maximum_attempts: int = 1,
        maximum_backoff: timedelta = timedelta(hours=1),
    ) -> datetime | None:
        with self._database.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, attempt_count FROM summary_jobs WHERE id = ?",
                (job.id,),
            ).fetchone()
            if row is None:
                raise KeyError(job.id)
            if str(row[0]) == "completed":
                return None
            attempts = min(int(row[1]) + 1, maximum_attempts)
            if attempts >= maximum_attempts:
                next_retry_at = None
            else:
                delay = min(timedelta(minutes=2 ** (attempts - 1)), maximum_backoff)
                next_retry_at = now + delay
            conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'failed', attempt_count = ?, next_retry_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    attempts,
                    None if next_retry_at is None else _timestamp(next_retry_at),
                    _timestamp(now),
                    job.id,
                ),
            )
        return next_retry_at


def _safe_usage_value(usage: dict[str, int], key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _usage_is_reported(usage: dict[str, int] | None) -> bool:
    return bool(
        usage is not None
        and any(key in usage for key in ("input_tokens", "output_tokens"))
    )
