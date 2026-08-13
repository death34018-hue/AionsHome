"""Short-lived, synchronous SQLite access for Visitor Lounge state."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text("utf-8")
        with self.connection() as conn:
            conn.executescript(schema)
        generation_job_columns = {
            "quota_window_id": "TEXT REFERENCES quota_windows(id) ON DELETE SET NULL",
            "response_message_id": "TEXT REFERENCES messages(id) ON DELETE SET NULL",
            "confirmed_at": "TEXT",
            "refunded_at": "TEXT",
            "refund_reason": "TEXT",
            "action": (
                "TEXT CHECK(action IN ("
                "'continue', 'closing', 'end', 'suspend', 'safety_lock'))"
            ),
        }
        visitor_columns = {
            "disclosure_version": "TEXT",
            "disclosure_consented_at": "TEXT",
            "last_summarized_message_id": (
                "TEXT REFERENCES messages(id) ON DELETE SET NULL"
            ),
            "visitor_kind": (
                "TEXT NOT NULL DEFAULT 'human' "
                "CHECK(visitor_kind IN ('human', 'external_ai'))"
            ),
            "safety_locked_until": "TEXT",
        }
        message_columns = {
            "source": (
                "TEXT NOT NULL DEFAULT 'web' "
                "CHECK(source IN ('web', 'mcp'))"
            ),
            "delivery_status": (
                "TEXT NOT NULL DEFAULT 'accepted' "
                "CHECK(delivery_status IN ('accepted', 'failed'))"
            ),
        }
        with self.transaction(immediate=True) as conn:
            existing = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(generation_jobs)").fetchall()
            }
            for name, definition in generation_job_columns.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE generation_jobs ADD COLUMN {name} {definition}"
                    )
            existing_visitors = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(visitors)").fetchall()
            }
            for name, definition in visitor_columns.items():
                if name not in existing_visitors:
                    conn.execute(f"ALTER TABLE visitors ADD COLUMN {name} {definition}")
            existing_messages = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            for name, definition in message_columns.items():
                if name not in existing_messages:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            existing_reception_settings = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(reception_settings)"
                ).fetchall()
            }
            if "credential_detected" not in existing_reception_settings:
                conn.execute(
                    "ALTER TABLE reception_settings "
                    "ADD COLUMN credential_detected TEXT"
                )
            if "hourly_quota_limit" not in existing_reception_settings:
                conn.execute(
                    "ALTER TABLE reception_settings "
                    "ADD COLUMN hourly_quota_limit INTEGER NOT NULL DEFAULT 15 "
                    "CHECK(hourly_quota_limit BETWEEN 1 AND 500)"
                )
            existing_model_calls = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(model_calls)").fetchall()
            }
            if "usage_reported" not in existing_model_calls:
                conn.execute(
                    """
                    ALTER TABLE model_calls ADD COLUMN usage_reported
                    INTEGER NOT NULL DEFAULT 0 CHECK(usage_reported IN (0, 1))
                    """
                )
                conn.execute(
                    """
                    UPDATE model_calls SET usage_reported = 1
                    WHERE input_tokens > 0 OR output_tokens > 0
                    """
                )
            existing_visits = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(visits)").fetchall()
            }
            if "last_activity_at" not in existing_visits:
                conn.execute("ALTER TABLE visits ADD COLUMN last_activity_at TEXT")
                conn.execute(
                    "UPDATE visits SET last_activity_at = started_at "
                    "WHERE last_activity_at IS NULL"
                )
            conn.execute(
                """
                DELETE FROM summaries
                WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM summaries GROUP BY visitor_id
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS summaries_one_per_visitor
                ON summaries(visitor_id)
                """
            )
            conn.execute(
                """
                UPDATE visitor_keys SET revoked_at = ?
                WHERE revoked_at IS NULL
                  AND rowid NOT IN (
                    SELECT MAX(rowid) FROM visitor_keys
                    WHERE revoked_at IS NULL GROUP BY visitor_id
                  )
                """,
                (utc_now().isoformat(),),
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS visitor_keys_one_active_per_visitor
                ON visitor_keys(visitor_id) WHERE revoked_at IS NULL
                """
            )
            quota_cutoff = utc_now() - timedelta(hours=12)
            latest_windows = conn.execute(
                """
                SELECT id, started_at FROM quota_windows
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM quota_windows GROUP BY visitor_id
                )
                  AND julianday(started_at) > julianday(?)
                """,
                (quota_cutoff.isoformat(),),
            ).fetchall()
            for window_id, started_at_text in latest_windows:
                started_at = datetime.fromisoformat(str(started_at_text))
                if started_at.tzinfo is None or started_at.utcoffset() is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                else:
                    started_at = started_at.astimezone(timezone.utc)
                conn.execute(
                    "UPDATE quota_windows SET ends_at = ? WHERE id = ?",
                    ((started_at + timedelta(hours=12)).isoformat(), window_id),
                )
        self._upgrade_generation_job_status_constraint()
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO summary_generation_attempts
                    (id, summary_job_id, visitor_id, status, usage_reported,
                     input_tokens, output_tokens, failure_reason, started_at,
                     finished_at)
                SELECT 'legacy-summary-attempt:' || summary_jobs.id,
                       summary_jobs.id,
                       summary_jobs.visitor_id,
                       CASE summary_jobs.status
                         WHEN 'completed' THEN 'completed'
                         WHEN 'running' THEN 'running'
                         ELSE 'failed'
                       END,
                       CASE
                         WHEN summary_jobs.input_tokens > 0
                           OR summary_jobs.output_tokens > 0 THEN 1
                         ELSE 0
                       END,
                       summary_jobs.input_tokens,
                       summary_jobs.output_tokens,
                       CASE
                         WHEN summary_jobs.status = 'failed'
                           THEN 'legacy_summary_failure'
                         ELSE NULL
                       END,
                       summary_jobs.started_at,
                       CASE
                         WHEN summary_jobs.status = 'running' THEN NULL
                         ELSE COALESCE(
                           summary_jobs.finished_at, summary_jobs.started_at
                         )
                       END
                FROM summary_jobs
                WHERE summary_jobs.started_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM summary_generation_attempts
                    WHERE summary_generation_attempts.summary_job_id = summary_jobs.id
                  )
                """
            )

    def _upgrade_generation_job_status_constraint(self) -> None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'generation_jobs'"
            ).fetchone()
        table_sql = "" if row is None or row[0] is None else str(row[0])
        normalized = "".join(table_sql.casefold().split())
        status_current = (
            "check(statusin(" not in normalized or "'interrupted'" in normalized
        )
        actions_current = (
            "check(actionin(" not in normalized
            or ("'closing'" in normalized and "'end'" in normalized)
        )
        if status_current and actions_current:
            return

        with self.connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE generation_jobs_replacement (
                        id TEXT PRIMARY KEY,
                        visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                        message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
                        response_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
                        quota_window_id TEXT REFERENCES quota_windows(id) ON DELETE SET NULL,
                        request_id TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL CHECK(kind IN ('chat', 'summary')),
                        status TEXT NOT NULL CHECK(status IN (
                            'queued', 'running', 'completed', 'failed', 'cancelled',
                            'interrupted'
                        )),
                        visible_text TEXT NOT NULL DEFAULT '',
                        action TEXT CHECK(action IN (
                            'continue', 'closing', 'end', 'suspend', 'safety_lock'
                        )),
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        confirmed_at TEXT,
                        refunded_at TEXT,
                        refund_reason TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO generation_jobs_replacement
                        (id, visitor_id, message_id, response_message_id,
                         quota_window_id, request_id, kind, status, visible_text,
                         action, created_at, started_at, finished_at, confirmed_at,
                         refunded_at, refund_reason)
                    SELECT id, visitor_id, message_id, response_message_id,
                           quota_window_id, request_id, kind, status, visible_text,
                           action, created_at, started_at, finished_at, confirmed_at,
                           refunded_at, refund_reason
                    FROM generation_jobs
                    """
                )
                conn.execute("DROP TABLE generation_jobs")
                conn.execute(
                    "ALTER TABLE generation_jobs_replacement RENAME TO generation_jobs"
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX generation_jobs_one_active_per_visitor
                    ON generation_jobs(visitor_id)
                    WHERE status IN ('queued', 'running')
                    """
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    "generation_jobs migration left foreign key violations"
                )

    def recover_stale_jobs(self, now: datetime | None = None) -> int:
        """Interrupt abandoned chat jobs and refund every incomplete generation."""
        recovered_at = (now or utc_now()).isoformat()
        with self.transaction(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT id, quota_window_id, confirmed_at, refunded_at, message_id
                FROM generation_jobs
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()
            for (
                job_id,
                window_id,
                confirmed_at,
                refunded_at,
                message_id,
            ) in rows:
                if window_id is not None and refunded_at is None:
                    column = "used_count" if confirmed_at is not None else "reserved_count"
                    conn.execute(
                        f"""
                        UPDATE quota_windows
                        SET {column} = {column} - 1
                        WHERE id = ? AND {column} > 0
                        """,
                        (window_id,),
                    )
                    conn.execute(
                        """
                        UPDATE generation_jobs
                        SET refunded_at = ?, refund_reason = 'startup_recovery'
                        WHERE id = ?
                        """,
                        (recovered_at, job_id),
                    )
                if message_id is not None:
                    conn.execute(
                        """
                        UPDATE messages SET delivery_status = 'failed'
                        WHERE id = ? AND sender = 'visitor'
                        """,
                        (message_id,),
                    )
                conn.execute(
                    """
                    UPDATE model_calls
                    SET completed_at = ?
                    WHERE job_id = ? AND completed_at IS NULL
                    """,
                    (recovered_at, job_id),
                )
                conn.execute(
                    """
                    UPDATE generation_jobs
                    SET status = 'interrupted', response_message_id = NULL,
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (recovered_at, job_id),
                )
            return len(rows)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
