from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from visitor_lounge.quota import (
    QuotaExhausted,
    QuotaService,
    RefundNotAllowed,
    RequestConflict,
)
from visitor_lounge.repository import MessageRepository, VisitorRepository


NOW = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def visitor_a(database):
    database.initialize()
    return VisitorRepository(database).create_unclaimed_visitor()


@pytest.fixture
def visitor_b(database, visitor_a):
    del visitor_a
    return VisitorRepository(database).create_unclaimed_visitor()


@pytest.fixture
def quota(database, visitor_a):
    del visitor_a
    return QuotaService(database)


@pytest.fixture
def messages(database, visitor_a):
    del visitor_a
    return MessageRepository(database)


def _complete_job(database, job_id: str) -> None:
    """Simulate the later queue task completing a persisted generation job."""
    with database.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = 'completed' WHERE id = ?",
            (job_id,),
        )


def test_initialize_upgrades_an_existing_generation_jobs_table(database):
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
                message_id TEXT,
                request_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                visible_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            """
        )

    database.initialize()
    visitor_id = VisitorRepository(database).create_unclaimed_visitor()

    reservation = QuotaService(database).reserve(visitor_id, "request-1", NOW)

    assert reservation.visitor_id == visitor_id


def test_duplicate_request_reserves_only_once(quota, visitor_a):
    first = quota.reserve(visitor_a, "request-1", NOW)

    second = quota.reserve(visitor_a, "request-1", NOW)

    assert second == first
    assert quota.state(visitor_a).reserved == 1


def test_request_id_cannot_be_reused_by_another_visitor(quota, visitor_a, visitor_b):
    quota.reserve(visitor_a, "request-1", NOW)

    with pytest.raises(RequestConflict):
        quota.reserve(visitor_b, "request-1", NOW)


def test_concurrent_duplicate_submission_has_one_reservation(database, quota, visitor_a):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(quota.reserve, visitor_a, "request-1", NOW)
            for _ in range(2)
        ]
        reservations = [future.result() for future in futures]

    assert reservations[0] == reservations[1]
    assert quota.state(visitor_a).reserved == 1
    with database.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM generation_jobs WHERE request_id = ?",
            ("request-1",),
        ).fetchone()[0]
    assert count == 1


def test_confirm_moves_reserved_to_used_only_once(quota, visitor_a):
    quota.reserve(visitor_a, "request-1", NOW)

    first = quota.confirm("request-1")
    second = quota.confirm("request-1")

    assert first == second
    assert (second.reserved, second.used) == (0, 1)


def test_refund_is_applied_at_most_once(quota, visitor_a):
    quota.reserve(visitor_a, "request-1", NOW)
    quota.confirm("request-1")

    quota.refund_once("request-1", "no_visible_reply")
    quota.refund_once("request-1", "duplicate_callback")

    assert quota.state(visitor_a).used == 0


def test_visible_reply_prevents_refund(database, quota, visitor_a):
    quota.reserve(visitor_a, "request-1", NOW)
    quota.confirm("request-1")
    with database.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE generation_jobs
            SET visible_text = ?
            WHERE request_id = ?
            """,
            ("A visible reply", "request-1"),
        )

    with pytest.raises(RefundNotAllowed, match="visible reply cannot be refunded"):
        quota.refund_once("request-1", "late_failure_callback")

    state = quota.state(visitor_a)
    with database.connection() as conn:
        refund = conn.execute(
            """
            SELECT refunded_at, refund_reason
            FROM generation_jobs
            WHERE request_id = ?
            """,
            ("request-1",),
        ).fetchone()
    assert (state.reserved, state.used) == (0, 1)
    assert refund == (None, None)


def test_refund_before_confirm_prevents_later_charge(quota, visitor_a):
    quota.reserve(visitor_a, "request-1", NOW)

    quota.refund_once("request-1", "cancelled_before_start")
    state = quota.confirm("request-1")

    assert (state.reserved, state.used) == (0, 0)


def test_starting_after_expiry_creates_a_fresh_24_hour_window(
    database, quota, visitor_a
):
    first = quota.reserve(visitor_a, "request-1", NOW)
    quota.confirm("request-1")
    _complete_job(database, first.job_id)

    second = quota.reserve(visitor_a, "request-2", NOW + timedelta(hours=24))
    state = quota.state(visitor_a)

    assert second.window_id != first.window_id
    assert state.started_at == NOW + timedelta(hours=24)
    assert state.ends_at == NOW + timedelta(hours=48)
    assert (state.used, state.reserved) == (0, 1)


def test_quota_follows_visitor_id_across_caller_context_changes(
    database, quota, visitor_a
):
    first = quota.reserve(visitor_a, "request-from-old-key", NOW)
    quota.confirm("request-from-old-key")
    _complete_job(database, first.job_id)

    quota.reserve(visitor_a, "request-from-new-key-cookie-and-ip", NOW + timedelta(minutes=1))

    assert (quota.state(visitor_a).used, quota.state(visitor_a).reserved) == (1, 1)


def test_exhausted_quota_does_not_persist_an_unaccepted_message(
    database, quota, messages, visitor_a
):
    with database.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count, started_at, ends_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "full-window",
                visitor_a,
                10,
                10,
                0,
                NOW.isoformat(),
                (NOW + timedelta(hours=24)).isoformat(),
            ),
        )

    with pytest.raises(QuotaExhausted) as error:
        quota.reserve_message(visitor_a, "request-1", "not accepted", NOW)

    assert error.value.ends_at == NOW + timedelta(hours=24)
    assert messages.recent(visitor_a) == []


def test_duplicate_accepted_message_is_persisted_with_reservation_once(
    database, quota, messages, visitor_a
):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                quota.reserve_message,
                visitor_a,
                "request-1",
                "hello",
                NOW,
            )
            for _ in range(2)
        ]
        reservations = [future.result() for future in futures]

    assert reservations[0] == reservations[1]
    assert [message.content for message in messages.recent(visitor_a)] == ["hello"]
    with database.connection() as conn:
        row = conn.execute(
            "SELECT message_id FROM generation_jobs WHERE request_id = ?",
            ("request-1",),
        ).fetchone()
    assert row[0] == messages.recent(visitor_a)[0].id


def test_recent_messages_cannot_cross_visitor(messages, visitor_a, visitor_b):
    messages.append_message(visitor_a, "visitor", "A secret", created_at=NOW)
    messages.append_message(visitor_b, "visitor", "B secret", created_at=NOW)

    assert [message.content for message in messages.recent_messages(visitor_a)] == [
        "A secret"
    ]


def test_recent_returns_latest_ten_in_display_order(messages, visitor_a):
    for number in range(12):
        messages.append(
            visitor_a,
            "host" if number % 2 else "visitor",
            f"message-{number}",
            created_at=NOW + timedelta(seconds=number),
        )

    recent = messages.recent(visitor_a)

    assert [message.content for message in recent] == [
        f"message-{number}" for number in range(2, 12)
    ]
