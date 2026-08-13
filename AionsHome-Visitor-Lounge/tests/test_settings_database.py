from dataclasses import replace
from datetime import timezone
import sqlite3
import tomllib
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import pytest

from visitor_lounge.container import Container
from visitor_lounge.database import utc_now
from visitor_lounge.settings import Settings


def _set_valid_settings_environment(monkeypatch) -> None:
    monkeypatch.setenv("VISITOR_LOUNGE_KEY_PEPPER", "p" * 32)
    monkeypatch.setenv("VISITOR_LOUNGE_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VISITOR_LOUNGE_SESSION_SECRET", "s" * 32)


def _settings_project(
    tmp_path, project_root, replacements: dict[str, str]
):
    config = (project_root / "config" / "visitor-lounge.toml").read_text("utf-8")
    for original, replacement in replacements.items():
        assert original in config
        config = config.replace(original, replacement, 1)
    root = tmp_path / "settings-project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "visitor-lounge.toml").write_text(config, encoding="utf-8")
    return root


def test_settings_rejects_public_bind_and_excess_concurrency(project_root, monkeypatch):
    monkeypatch.setenv("VISITOR_LOUNGE_KEY_PEPPER", "p" * 32)
    monkeypatch.setenv("VISITOR_LOUNGE_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VISITOR_LOUNGE_SESSION_SECRET", "s" * 32)

    settings = Settings.load(project_root)

    assert settings.visitor_host == "127.0.0.1"
    assert settings.admin_host == "127.0.0.1"
    assert settings.max_generations == 1
    assert settings.max_generations_hard_limit == 2
    assert settings.max_waiting == 3


def test_settings_does_not_require_or_expose_a_lounge_codex_home(
    project_root, monkeypatch
):
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.delenv("VISITOR_LOUNGE_CODEX_HOME", raising=False)

    settings = Settings.load(project_root)

    assert "codex_home" not in Settings.__dataclass_fields__
    assert settings.codex_workdir == project_root / ".runtime/codex-workdir"


@pytest.mark.parametrize(
    ("replacement", "environment_override", "expected_error"),
    [
        pytest.param(
            {},
            {"VISITOR_LOUNGE_MASTER_KEY": "invalid-fernet-key-DO-NOT-PRINT"},
            "invalid visitor lounge master key",
            id="invalid_master_key",
        ),
        pytest.param(
            {'timezone = "Asia/Shanghai"': 'timezone = "Invalid/NoSuchZone"'},
            {},
            "invalid admin timezone",
            id="invalid_timezone",
        ),
        pytest.param(
            {"max_generations = 1": "max_generations = 0"},
            {},
            "max_generations must be between 1 and 2",
            id="zero_generations",
        ),
        pytest.param(
            {"max_generations = 1": "max_generations = 3"},
            {},
            "max_generations must be between 1 and 2",
            id="excess_generations",
        ),
        pytest.param(
            {"max_waiting = 3": "max_waiting = -1"},
            {},
            "max_waiting must be between 0 and 3",
            id="negative_waiting",
        ),
    ],
)
def test_settings_load_rejects_invalid_runtime_configuration(
    tmp_path,
    project_root,
    monkeypatch,
    replacement,
    environment_override,
    expected_error,
):
    _set_valid_settings_environment(monkeypatch)
    for name, value in environment_override.items():
        monkeypatch.setenv(name, value)
    root = _settings_project(tmp_path, project_root, replacement)

    with pytest.raises(ValueError) as raised:
        Settings.load(root)

    assert str(raised.value) == expected_error
    assert "invalid-fernet-key-DO-NOT-PRINT" not in str(raised.value)


def test_settings_validate_rejects_an_invalid_constructed_instance(
    project_root, monkeypatch
):
    _set_valid_settings_environment(monkeypatch)
    settings = Settings.load(project_root)

    with pytest.raises(ValueError):
        replace(settings, max_generations=0).validate()


@pytest.mark.parametrize(
    ("max_generations", "max_waiting", "expected_waiting"),
    [(1, 0, 0), (2, 3, 3), (2, 99, 3)],
)
def test_settings_preserves_valid_queue_boundaries_and_waiting_cap(
    tmp_path,
    project_root,
    monkeypatch,
    max_generations,
    max_waiting,
    expected_waiting,
):
    _set_valid_settings_environment(monkeypatch)
    root = _settings_project(
        tmp_path,
        project_root,
        {
            "max_generations = 1": f"max_generations = {max_generations}",
            "max_waiting = 3": f"max_waiting = {max_waiting}",
        },
    )

    settings = Settings.load(root)

    assert settings.max_generations == max_generations
    assert settings.max_waiting == expected_waiting


def test_default_admin_timezone_has_a_windows_runtime_database(project_root):
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text("utf-8"))
    dependencies = metadata["project"]["dependencies"]

    assert any(dependency.casefold().startswith("tzdata") for dependency in dependencies)
    assert Settings.__dataclass_fields__["timezone_name"].default == "Asia/Shanghai"
    assert ZoneInfo("Asia/Shanghai").key == "Asia/Shanghai"


def test_schema_enables_wal_foreign_keys_and_all_visitor_tables(database):
    database.initialize()

    with database.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert {
        "visitors",
        "visitor_keys",
        "auth_sessions",
        "visits",
        "quota_windows",
        "messages",
        "summaries",
        "summary_generation_attempts",
        "generation_jobs",
        "model_calls",
        "notification_events",
        "audit_events",
    } <= tables


def test_schema_rejects_visitor_dependent_rows_without_a_visitor(database):
    database.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO visitor_keys
                    (id, visitor_id, key_hash, encrypted_value, masked, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("key-1", "missing-visitor", b"hash", b"encrypted", "***", "now"),
            )


def test_initialize_backfills_legacy_summary_attempts_idempotently_and_cascades(
    database,
):
    with database.connection() as conn:
        conn.executescript(
            """
            CREATE TABLE visitors (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE summary_jobs (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                first_message_id TEXT NOT NULL REFERENCES messages(id),
                last_message_id TEXT NOT NULL REFERENCES messages(id),
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            """
        )
        for suffix in ("completed", "failed", "running"):
            conn.execute(
                "INSERT INTO visitors (id, created_at) VALUES (?, ?)",
                (f"visitor-{suffix}", "2026-08-06T08:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO messages
                    (id, visitor_id, sender, content, created_at)
                VALUES (?, ?, 'visitor', 'legacy message', ?)
                """,
                (
                    f"message-{suffix}",
                    f"visitor-{suffix}",
                    "2026-08-06T08:10:00+00:00",
                ),
            )
        conn.executemany(
            """
            INSERT INTO summary_jobs
                (id, visitor_id, first_message_id, last_message_id, status,
                 input_tokens, output_tokens, created_at, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "job-completed",
                    "visitor-completed",
                    "message-completed",
                    "message-completed",
                    "completed",
                    400,
                    100,
                    "2026-08-06T08:00:00+00:00",
                    "2026-08-06T08:20:00+00:00",
                    "2026-08-06T08:20:02+00:00",
                ),
                (
                    "job-failed",
                    "visitor-failed",
                    "message-failed",
                    "message-failed",
                    "failed",
                    17,
                    0,
                    "2026-08-06T08:00:00+00:00",
                    "2026-08-06T08:30:00+00:00",
                    "2026-08-06T08:30:03+00:00",
                ),
                (
                    "job-running",
                    "visitor-running",
                    "message-running",
                    "message-running",
                    "running",
                    0,
                    0,
                    "2026-08-06T08:00:00+00:00",
                    "2026-08-06T08:40:00+00:00",
                    None,
                ),
            ],
        )
        conn.commit()

    database.initialize()
    database.initialize()

    with database.connection() as conn:
        attempts = conn.execute(
            """
            SELECT id, summary_job_id, status, usage_reported,
                   input_tokens, output_tokens, started_at, finished_at,
                   failure_reason
            FROM summary_generation_attempts ORDER BY summary_job_id
            """
        ).fetchall()
    assert attempts == [
        (
            "legacy-summary-attempt:job-completed",
            "job-completed",
            "completed",
            1,
            400,
            100,
            "2026-08-06T08:20:00+00:00",
            "2026-08-06T08:20:02+00:00",
            None,
        ),
        (
            "legacy-summary-attempt:job-failed",
            "job-failed",
            "failed",
            1,
            17,
            0,
            "2026-08-06T08:30:00+00:00",
            "2026-08-06T08:30:03+00:00",
            "legacy_summary_failure",
        ),
        (
            "legacy-summary-attempt:job-running",
            "job-running",
            "running",
            0,
            0,
            0,
            "2026-08-06T08:40:00+00:00",
            None,
            None,
        ),
    ]

    with database.transaction(immediate=True) as conn:
        conn.execute("DELETE FROM visitors WHERE id = 'visitor-completed'")
    with database.connection() as conn:
        remaining = conn.execute(
            "SELECT summary_job_id FROM summary_generation_attempts ORDER BY summary_job_id"
        ).fetchall()
    assert remaining == [("job-failed",), ("job-running",)]


def test_generation_jobs_reject_duplicate_request_ids(database):
    database.initialize()

    with database.transaction() as conn:
        conn.executemany(
            "INSERT INTO visitors (id, created_at) VALUES (?, ?)",
            [("visitor-1", "now"), ("visitor-2", "now")],
        )
        conn.execute(
            """
            INSERT INTO generation_jobs (id, visitor_id, request_id, kind, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("job-1", "visitor-1", "request-1", "chat", "completed", "now"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO generation_jobs (id, visitor_id, request_id, kind, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("job-2", "visitor-2", "request-1", "chat", "completed", "now"),
            )


def test_generation_jobs_allow_only_one_active_job_per_visitor(database):
    database.initialize()

    with database.transaction() as conn:
        conn.execute("INSERT INTO visitors (id, created_at) VALUES (?, ?)", ("visitor-1", "now"))
        conn.execute(
            """
            INSERT INTO generation_jobs (id, visitor_id, request_id, kind, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("job-1", "visitor-1", "request-1", "chat", "queued", "now"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO generation_jobs (id, visitor_id, request_id, kind, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("job-2", "visitor-1", "request-2", "chat", "running", "now"),
            )


def test_transaction_commits_and_rolls_back_a_real_database_change(database):
    database.initialize()

    with database.transaction(immediate=True) as conn:
        conn.execute("INSERT INTO visitors (id, created_at) VALUES (?, ?)", ("committed", "now"))

    try:
        with database.transaction() as conn:
            conn.execute("INSERT INTO visitors (id, created_at) VALUES (?, ?)", ("rolled-back", "now"))
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    with database.connection() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM visitors")}
    assert ids == {"committed"}


def test_container_builds_local_database_and_utc_clock(project_root, monkeypatch):
    monkeypatch.setenv("VISITOR_LOUNGE_KEY_PEPPER", "p" * 32)
    monkeypatch.setenv("VISITOR_LOUNGE_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VISITOR_LOUNGE_SESSION_SECRET", "s" * 32)

    settings = Settings.load(project_root)
    container = Container.build(settings)

    assert container.settings is settings
    assert container.database.path == settings.database_path
    assert utc_now().tzinfo is timezone.utc


def test_model_call_usage_migration_is_conservative_and_idempotent(database):
    with database.connection() as conn:
        conn.executescript(
            """
            CREATE TABLE visitors (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE generation_jobs (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('chat', 'summary')),
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'running', 'completed', 'failed', 'cancelled',
                    'interrupted'
                )),
                visible_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE model_calls (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                job_id TEXT NOT NULL REFERENCES generation_jobs(id) ON DELETE CASCADE,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            INSERT INTO visitors (id, created_at) VALUES ('visitor', '2026-08-06');
            INSERT INTO generation_jobs
                (id, visitor_id, request_id, kind, status, created_at)
            VALUES
                ('job-nonzero', 'visitor', 'request-nonzero', 'chat', 'completed', '2026-08-06'),
                ('job-zero', 'visitor', 'request-zero', 'chat', 'completed', '2026-08-06');
            INSERT INTO model_calls
                (id, visitor_id, job_id, input_tokens, output_tokens, created_at)
            VALUES
                ('call-nonzero', 'visitor', 'job-nonzero', 12, 3, '2026-08-06'),
                ('call-zero', 'visitor', 'job-zero', 0, 0, '2026-08-06');
            """
        )
        conn.commit()

    database.initialize()
    database.initialize()

    with database.connection() as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(model_calls)")
        }
        calls = conn.execute(
            """
            SELECT id, usage_reported, input_tokens, output_tokens
            FROM model_calls ORDER BY id
            """
        ).fetchall()
    assert "usage_reported" in columns
    assert calls == [
        ("call-nonzero", 1, 12, 3),
        ("call-zero", 0, 0, 0),
    ]
