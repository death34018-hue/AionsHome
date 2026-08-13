"""Persistent, visitor-scoped accounting for the generation quota window."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

from visitor_lounge.database import Database, utc_now
from visitor_lounge.models import MessageSource, QuotaReservation, QuotaState
from visitor_lounge.repository import VisitorNotFound, _insert_message


QUOTA_LIMIT = 15
MAX_QUOTA_LIMIT = 500
QUOTA_WINDOW = timedelta(hours=12)


class RequestConflict(ValueError):
    """Raised when a request ID already belongs to a different visitor."""


class VisitorHasPendingJob(RuntimeError):
    """Raised before persistence when a visitor already has active work."""


class ReservationNotFound(ValueError):
    """Raised when a request ID has no quota reservation."""


class RefundNotAllowed(RuntimeError):
    """Raised when a visible reply makes a confirmed charge non-refundable."""


class QuotaExhausted(RuntimeError):
    """Raised when a visitor has consumed and reserved the full window."""

    def __init__(self, ends_at: datetime) -> None:
        super().__init__(f"quota exhausted until {ends_at.isoformat()}")
        self.ends_at = ends_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


class QuotaService:
    """Reserve, confirm, and refund quota with durable idempotency guards."""

    def __init__(
        self,
        database: Database,
        limit_provider: Callable[[], int] | None = None,
    ) -> None:
        self._database = database
        self._limit_provider = limit_provider or (lambda: QUOTA_LIMIT)

    def configured_limit(self) -> int:
        limit = int(self._limit_provider())
        if not 1 <= limit <= MAX_QUOTA_LIMIT:
            raise ValueError("quota window limit must be between 1 and 500")
        return limit

    def reserve(
        self, visitor_id: str, request_id: str, now: datetime
    ) -> QuotaReservation:
        return self._reserve(
            visitor_id, request_id, now, message=None, message_source="web"
        )

    def reserve_message(
        self,
        visitor_id: str,
        request_id: str,
        content: str,
        now: datetime,
        *,
        source: MessageSource = "web",
    ) -> QuotaReservation:
        """Persist an accepted visitor message and its reservation atomically."""
        return self._reserve(
            visitor_id, request_id, now, message=content, message_source=source
        )

    def _reserve(
        self,
        visitor_id: str,
        request_id: str,
        now: datetime,
        *,
        message: str | None,
        message_source: MessageSource,
    ) -> QuotaReservation:
        now = _utc(now)
        with self._database.transaction(immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT id, request_id, visitor_id, quota_window_id
                FROM generation_jobs
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[2]) != visitor_id:
                    raise RequestConflict(request_id)
                return self._reservation_from_row(existing)

            window = self._active_or_new_window(conn, visitor_id, now)
            if int(window[2]) + int(window[3]) >= int(window[1]):
                raise QuotaExhausted(datetime.fromisoformat(str(window[5])))

            message_id = None
            if message is not None:
                persisted = _insert_message(
                    conn,
                    visitor_id,
                    "visitor",
                    message,
                    now,
                    source=message_source,
                )
                message_id = persisted.id

            reservation = QuotaReservation(
                job_id=str(uuid4()),
                request_id=request_id,
                visitor_id=visitor_id,
                window_id=str(window[0]),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO generation_jobs
                        (id, visitor_id, message_id, quota_window_id, request_id,
                         kind, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'chat', 'queued', ?)
                    """,
                    (
                        reservation.job_id,
                        reservation.visitor_id,
                        message_id,
                        reservation.window_id,
                        reservation.request_id,
                        _timestamp(now),
                    ),
                )
            except sqlite3.IntegrityError as error:
                active = conn.execute(
                    """
                    SELECT 1 FROM generation_jobs
                    WHERE visitor_id = ? AND status IN ('queued', 'running')
                    """,
                    (visitor_id,),
                ).fetchone()
                if active is not None:
                    raise VisitorHasPendingJob(visitor_id) from error
                raise
            conn.execute(
                """
                UPDATE quota_windows
                SET reserved_count = reserved_count + 1
                WHERE id = ?
                """,
                (reservation.window_id,),
            )
            return reservation

    def cancel_unsubmitted(self, visitor_id: str, request_id: str) -> bool:
        """Undo a reservation that the scheduler did not accept."""
        with self._database.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT id, message_id, quota_window_id, status, confirmed_at,
                       visible_text
                FROM generation_jobs
                WHERE request_id = ? AND visitor_id = ?
                """,
                (request_id, visitor_id),
            ).fetchone()
            if (
                row is None
                or str(row[3]) != "queued"
                or row[4] is not None
                or str(row[5]) != ""
            ):
                return False
            conn.execute("DELETE FROM generation_jobs WHERE id = ?", (row[0],))
            if row[1] is not None:
                conn.execute("DELETE FROM messages WHERE id = ?", (row[1],))
            conn.execute(
                """
                UPDATE quota_windows
                SET reserved_count = reserved_count - 1
                WHERE id = ? AND reserved_count > 0
                """,
                (row[2],),
            )
            conn.execute(
                """
                DELETE FROM quota_windows
                WHERE id = ? AND used_count = 0 AND reserved_count = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM generation_jobs
                    WHERE generation_jobs.quota_window_id = quota_windows.id
                  )
                """,
                (row[2],),
            )
            return True

    def confirm(self, request_id: str) -> QuotaState:
        with self._database.transaction(immediate=True) as conn:
            row = self._reservation_row(conn, request_id)
            if row[5] is None and row[6] is None:
                conn.execute(
                    """
                    UPDATE quota_windows
                    SET reserved_count = reserved_count - 1,
                        used_count = used_count + 1
                    WHERE id = ? AND reserved_count > 0
                    """,
                    (row[3],),
                )
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET confirmed_at = ?
                    WHERE id = ? AND confirmed_at IS NULL AND refunded_at IS NULL
                    """,
                    (_timestamp(utc_now()), row[0]),
                )
            return self._state_for_window(conn, str(row[3]))

    def refund_once(self, request_id: str, reason: str) -> QuotaState:
        with self._database.transaction(immediate=True) as conn:
            row = self._reservation_row(conn, request_id)
            if row[6] is None and row[5] is not None and str(row[7]) != "":
                raise RefundNotAllowed("visible reply cannot be refunded")
            refunded = conn.execute(
                """
                UPDATE generation_jobs
                SET refunded_at = ?, refund_reason = ?
                WHERE id = ? AND refunded_at IS NULL
                """,
                (_timestamp(utc_now()), reason, row[0]),
            )
            if refunded.rowcount == 1:
                column = "used_count" if row[5] is not None else "reserved_count"
                conn.execute(
                    f"""
                    UPDATE quota_windows
                    SET {column} = {column} - 1
                    WHERE id = ? AND {column} > 0
                    """,
                    (row[3],),
                )
            return self._state_for_window(conn, str(row[3]))

    def refund_failed_generation(self, request_id: str, reason: str) -> QuotaState:
        """Refund one failed generation even if it streamed partial text."""
        with self._database.transaction(immediate=True) as conn:
            row = self._reservation_row(conn, request_id)
            refunded = conn.execute(
                """
                UPDATE generation_jobs
                SET refunded_at = ?, refund_reason = ?
                WHERE id = ? AND refunded_at IS NULL
                """,
                (_timestamp(utc_now()), reason, row[0]),
            )
            if refunded.rowcount == 1:
                column = "used_count" if row[5] is not None else "reserved_count"
                conn.execute(
                    f"""
                    UPDATE quota_windows
                    SET {column} = {column} - 1
                    WHERE id = ? AND {column} > 0
                    """,
                    (row[3],),
                )
            return self._state_for_window(conn, str(row[3]))

    def state(self, visitor_id: str, now: datetime | None = None) -> QuotaState:
        current = _utc(now or utc_now())
        with self._database.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT id, limit_count, used_count, reserved_count, started_at, ends_at
                FROM quota_windows
                WHERE visitor_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
            if row is not None:
                row = self._normalize_window(conn, row, current)
        if row is None:
            raise VisitorNotFound(visitor_id)
        return self._state_from_row(row)

    def _active_or_new_window(
        self, conn: sqlite3.Connection, visitor_id: str, now: datetime
    ) -> tuple[object, ...]:
        if conn.execute(
            "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
        ).fetchone() is None:
            raise VisitorNotFound(visitor_id)

        cutoff = _timestamp(now - QUOTA_WINDOW)
        row = conn.execute(
            """
            SELECT id, limit_count, used_count, reserved_count, started_at, ends_at
            FROM quota_windows
            WHERE visitor_id = ? AND started_at > ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (visitor_id, cutoff),
        ).fetchone()
        if row is not None:
            normalized = self._normalize_window(conn, row, now)
            if datetime.fromisoformat(str(normalized[5])) > now:
                return normalized

        window_id = str(uuid4())
        ends_at = now + QUOTA_WINDOW
        limit = self.configured_limit()
        conn.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES (?, ?, ?, 0, 0, ?, ?)
            """,
            (
                window_id,
                visitor_id,
                limit,
                _timestamp(now),
                _timestamp(ends_at),
            ),
        )
        return (window_id, limit, 0, 0, _timestamp(now), _timestamp(ends_at))

    def _normalize_window(
        self,
        conn: sqlite3.Connection,
        row: tuple[object, ...],
        now: datetime,
    ) -> tuple[object, ...]:
        limit = self.configured_limit()
        started_at = _utc(datetime.fromisoformat(str(row[4])))
        canonical_end = started_at + QUOTA_WINDOW
        canonical_end_text = _timestamp(canonical_end)
        if int(row[1]) != limit or str(row[5]) != canonical_end_text:
            conn.execute(
                "UPDATE quota_windows SET limit_count = ?, ends_at = ? WHERE id = ?",
                (limit, canonical_end_text, row[0]),
            )
        return (
            row[0],
            limit,
            row[2],
            row[3],
            row[4],
            canonical_end_text,
        )

    def _reservation_row(
        self, conn: sqlite3.Connection, request_id: str
    ) -> tuple[object, ...]:
        row = conn.execute(
            """
            SELECT id, request_id, visitor_id, quota_window_id, message_id,
                   confirmed_at, refunded_at, visible_text
            FROM generation_jobs
            WHERE request_id = ? AND quota_window_id IS NOT NULL
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise ReservationNotFound(request_id)
        return row

    @staticmethod
    def _reservation_from_row(row: tuple[object, ...]) -> QuotaReservation:
        if row[3] is None:
            raise ReservationNotFound(str(row[1]))
        return QuotaReservation(
            job_id=str(row[0]),
            request_id=str(row[1]),
            visitor_id=str(row[2]),
            window_id=str(row[3]),
        )

    def _state_for_window(
        self, conn: sqlite3.Connection, window_id: str
    ) -> QuotaState:
        row = conn.execute(
            """
            SELECT id, limit_count, used_count, reserved_count, started_at, ends_at
            FROM quota_windows
            WHERE id = ?
            """,
            (window_id,),
        ).fetchone()
        if row is None:
            raise ReservationNotFound(window_id)
        return self._state_from_row(row)

    @staticmethod
    def _state_from_row(row: tuple[object, ...]) -> QuotaState:
        return QuotaState(
            window_id=str(row[0]),
            limit=int(row[1]),
            used=int(row[2]),
            reserved=int(row[3]),
            started_at=datetime.fromisoformat(str(row[4])),
            ends_at=datetime.fromisoformat(str(row[5])),
        )
