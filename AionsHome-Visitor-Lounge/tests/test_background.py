import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import time

import pytest

from visitor_lounge.background import (
    BackgroundCoordinator,
    InvalidSummary,
    sanitize_summary,
)
from visitor_lounge.models import GenerationChunk, GenerationRequest, Message
from visitor_lounge.prompts import PromptBuilder
from visitor_lounge.quota import QuotaService
from visitor_lounge.repository import MessageRepository, VisitorRepository
from visitor_lounge.scheduler import GenerationScheduler


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
SAFE_HAN_SUMMARY = "访客提到近期安排和后续计划并希望以后继续交流具体进展" * 4


class RecordingSummaryScheduler:
    def __init__(self) -> None:
        self.work_calls = 0

    async def run_low_priority(self, work) -> bool:
        self.work_calls += 1
        await work()
        return True


class LeaseTrackingScheduler:
    def __init__(self) -> None:
        self.active = False
        self.released = asyncio.Event()

    async def run_low_priority(self, work) -> bool:
        self.active = True
        try:
            await work()
            return True
        finally:
            self.active = False
            self.released.set()


class PendingLeaseScheduler:
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_low_priority(self, work) -> bool:
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        await work()
        return True


class TimeoutAfterGeneratorStarts:
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started
        self.timeouts: list[float] = []

    async def __call__(self, tasks, timeout):
        self.timeouts.append(timeout)
        await self.started.wait()
        return set(), set(tasks)


@dataclass
class SchedulerSettings:
    max_waiting: int = 3
    queue_timeout_seconds: int = 120
    generation_timeout_seconds: int = 30


class ControlledChatAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: GenerationRequest):
        del request
        self.started.set()
        await self.release.wait()
        yield GenerationChunk(kind="completed")


def _chat_request(
    quota: QuotaService, visitor_id: str, number: int
) -> GenerationRequest:
    request_id = f"background-chat-request-{number}"
    reservation = quota.reserve(visitor_id, request_id, NOW)
    return GenerationRequest(
        job_id=reservation.job_id,
        request_id=request_id,
        visitor_id=visitor_id,
        message_id=f"background-chat-message-{number}",
        prompt="chat prompt",
    )


@pytest.fixture
def repositories(database):
    database.initialize()
    return VisitorRepository(database), MessageRepository(database)


@pytest.fixture
def visitors(repositories):
    visitor_repository, _ = repositories
    return (
        visitor_repository.create_unclaimed_visitor(),
        visitor_repository.create_unclaimed_visitor(),
    )


@pytest.fixture
def prompt_builder() -> PromptBuilder:
    return PromptBuilder(persona_text="温和接待来访者。", host_display_name="接待人")


@pytest.fixture
def background(database, prompt_builder) -> BackgroundCoordinator:
    return BackgroundCoordinator(database=database, prompt_builder=prompt_builder)


def _add_messages(
    messages: MessageRepository,
    visitor_id: str,
    *,
    visitor_count: int,
    started_at: datetime,
    host_between: bool = False,
):
    visitor_messages = []
    for number in range(visitor_count):
        visitor_messages.append(
            messages.append(
                visitor_id,
                "visitor",
                f"访客消息 {number}",
                created_at=started_at + timedelta(minutes=number),
            )
        )
        if host_between and number < visitor_count - 1:
            messages.append(
                visitor_id,
                "host",
                f"接待回复 {number}",
                created_at=started_at + timedelta(minutes=number, seconds=30),
            )
    return visitor_messages


def test_only_each_visitors_own_idle_timer_can_suspend_them(
    background, repositories, visitors
) -> None:
    visitor_repository, _ = repositories
    visitor_a, visitor_b = visitors
    background.record_activity(visitor_a, NOW - timedelta(minutes=30))
    background.record_activity(visitor_b, NOW - timedelta(minutes=29))

    result = background.tick(NOW)

    assert result.suspended == 1
    assert visitor_repository.visitor(visitor_a).status == "suspended"
    assert visitor_repository.visitor(visitor_b).status == "active"


def test_activity_resumes_suspended_visit_but_never_unlocks_safety_lock(
    background, repositories, visitors
) -> None:
    visitor_repository, _ = repositories
    visitor_a, visitor_b = visitors
    visitor_repository.set_status(visitor_a, "suspended")
    visitor_repository.set_status(visitor_b, "safety_lock")

    background.record_activity(visitor_a, NOW)
    background.record_activity(visitor_b, NOW)

    assert visitor_repository.visitor(visitor_a).status == "active"
    assert visitor_repository.visitor(visitor_b).status == "safety_lock"


def test_summary_freezes_first_ten_visitor_messages_and_keeps_host_context(
    background, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    visitor_messages = _add_messages(
        messages,
        visitor_a,
        visitor_count=11,
        started_at=NOW - timedelta(minutes=40),
        host_between=True,
    )

    jobs = background.enqueue_due_summaries(NOW)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.first_message_id == visitor_messages[0].id
    assert job.last_message_id == visitor_messages[9].id
    prompt = background.summary_prompt(job)
    assert "访客消息 9" in prompt
    assert "接待回复 8" in prompt
    assert "访客消息 10" not in prompt

    late = messages.append(visitor_a, "visitor", "生成期间到达", created_at=NOW)
    summary = background.complete_summary(
        job,
        ("访客分享了近期的生活安排，也提到正在处理的一件小事，希望之后继续聊聊进展。"
         "交流整体围绕当下近况、实际计划和仍待展开的话题，接待人给予了简短回应，并保留了下次自然续接的空间。"
         "双方还简要确认了目前没有需要立即处理的事项，之后可以根据新情况继续交流。"),
        usage={"input_tokens": 321, "output_tokens": 88},
        now=NOW + timedelta(minutes=1),
    )

    assert summary.last_message_id == visitor_messages[9].id
    assert background.last_summarized_message_id(visitor_a) == visitor_messages[9].id
    assert late.id != background.last_summarized_message_id(visitor_a)


def test_summary_requires_ten_visitor_messages_and_that_visitors_own_silence(
    background, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, visitor_b = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=35),
    )
    _add_messages(
        messages,
        visitor_b,
        visitor_count=20,
        started_at=NOW - timedelta(minutes=19),
    )

    jobs = background.enqueue_due_summaries(NOW)

    assert [job.visitor_id for job in jobs] == [visitor_a]


def test_summary_snapshot_never_reserves_live_chat_quota(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
    )

    jobs = background.enqueue_due_summaries(NOW)

    assert len(jobs) == 1
    assert scheduler.work_calls == 0
    with database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quota_windows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0


@pytest.mark.anyio
async def test_tick_dispatches_summary_through_atomic_low_priority_lease(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()

    async def generate(job, prompt):
        assert job.visitor_id == visitor_a
        assert "访客消息 9" in prompt
        return (
            "访客分享了近期的生活安排，也提到正在处理的一件小事，希望之后继续聊聊进展。"
            "交流整体围绕当下近况、实际计划和仍待展开的话题，接待人给予了简短回应，并保留了下次自然续接的空间。"
            "双方还简要确认了目前没有需要立即处理的事项，之后可以根据新情况继续交流。",
            {"input_tokens": 300, "output_tokens": 100},
        )

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=generate,
        clock=lambda: NOW,
    )

    tick = background.tick(NOW)
    await background.drain()

    assert len(tick.summary_jobs) == 1
    assert scheduler.work_calls == 1
    assert background.last_summarized_message_id(visitor_a) == (
        tick.summary_jobs[0].last_message_id
    )
    with database.connection() as connection:
        attempt = connection.execute(
            """
            SELECT summary_job_id, visitor_id, status, usage_reported,
                   input_tokens, output_tokens, started_at, finished_at
            FROM summary_generation_attempts
            """
        ).fetchone()
    assert attempt[:6] == (
        tick.summary_jobs[0].id,
        visitor_a,
        "completed",
        1,
        300,
        100,
    )
    assert attempt[6] == attempt[7] == NOW.isoformat()


@pytest.mark.anyio
async def test_stop_cancels_dispatch_waiting_to_acquire_low_priority_lease(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = PendingLeaseScheduler()
    generator_started = asyncio.Event()

    async def generate(job, prompt):
        del job, prompt
        generator_started.set()
        return SAFE_HAN_SUMMARY, {}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=generate,
        clock=lambda: NOW,
        stop_cleanup_timeout_seconds=0,
    )

    background.tick(NOW)
    await scheduler.waiting.wait()
    await background.stop()
    scheduler.release.set()
    await background.drain()

    assert scheduler.cancelled.is_set()
    assert not generator_started.is_set()
    assert background.last_summarized_message_id(visitor_a) is None


@pytest.mark.anyio
async def test_manual_tick_after_stop_does_not_start_new_generation(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    generator_started = asyncio.Event()

    async def generate(job, prompt):
        del job, prompt
        generator_started.set()
        return SAFE_HAN_SUMMARY, {}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=generate,
        clock=lambda: NOW,
    )

    await background.stop()
    tick = background.tick(NOW)
    await asyncio.sleep(0)
    await background.drain()

    assert len(tick.summary_jobs) == 1
    assert scheduler.work_calls == 0
    assert not generator_started.is_set()


@pytest.mark.anyio
async def test_old_epoch_stubborn_result_is_not_persisted_after_restart(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    generator_started = asyncio.Event()
    cancellation_suppressed = asyncio.Event()
    release_generator = asyncio.Event()

    async def stubborn_generator(job, prompt):
        del job, prompt
        generator_started.set()
        while not release_generator.is_set():
            try:
                await release_generator.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()
        return SAFE_HAN_SUMMARY, {}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=stubborn_generator,
        clock=lambda: NOW,
        stop_cleanup_timeout_seconds=0,
    )

    background.tick(NOW)
    await generator_started.wait()
    try:
        await background.stop()
        await cancellation_suppressed.wait()
        await background.start()
        release_generator.set()
        await background.drain()

        assert background.last_summarized_message_id(visitor_a) is None
        with database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM summaries"
            ).fetchone()[0]
        assert count == 0
    finally:
        release_generator.set()
        await background.stop()
        await background.drain()


@pytest.mark.anyio
async def test_hanging_summary_times_out_but_retains_lease_until_generator_ends(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = LeaseTrackingScheduler()
    generator_started = asyncio.Event()
    waiter = TimeoutAfterGeneratorStarts(generator_started)
    cancellation_suppressed = asyncio.Event()
    release_generator = asyncio.Event()

    async def hanging_generator(job, prompt):
        del job, prompt
        generator_started.set()
        while not release_generator.is_set():
            try:
                await release_generator.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()
        return (
            "不会保存的超时结果",
            {"input_tokens": 333, "output_tokens": 77},
        )

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=hanging_generator,
        clock=lambda: NOW,
        task_waiter=waiter,
    )

    tick = background.tick(NOW)
    try:
        await cancellation_suppressed.wait()

        assert waiter.timeouts == [120]
        assert scheduler.active is True
        assert not scheduler.released.is_set()
        assert background.last_summarized_message_id(visitor_a) is None
        with database.connection() as connection:
            status, attempts, retry_at = connection.execute(
                """
                SELECT status, attempt_count, next_retry_at
                FROM summary_jobs WHERE id = ?
                """,
                (tick.summary_jobs[0].id,),
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT status, usage_reported, input_tokens, output_tokens,
                       failure_reason
                FROM summary_generation_attempts
                """
            ).fetchone()
        assert (status, attempts) == ("failed", 1)
        assert datetime.fromisoformat(retry_at) == NOW + timedelta(minutes=1)
        assert attempt[:4] == ("timed_out", 0, 0, 0)
        assert attempt[4] == "timeout"
    finally:
        release_generator.set()
        await background.drain()
    assert scheduler.active is False
    assert scheduler.released.is_set()
    with database.connection() as connection:
        attempt = connection.execute(
            """
            SELECT status, usage_reported, input_tokens, output_tokens
            FROM summary_generation_attempts
            """
        ).fetchone()
        summary_count = connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    assert attempt == ("timed_out", 1, 333, 77)
    assert summary_count == 0
    assert background.last_summarized_message_id(visitor_a) is None


@pytest.mark.anyio
async def test_stop_marks_attempt_interrupted_before_late_result_adds_usage(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    generator_started = asyncio.Event()
    cancellation_suppressed = asyncio.Event()
    release_generator = asyncio.Event()

    async def cancellation_resistant_generator(job, prompt):
        del job, prompt
        generator_started.set()
        while not release_generator.is_set():
            try:
                await release_generator.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()
        return SAFE_HAN_SUMMARY, {"input_tokens": 222, "output_tokens": 66}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=cancellation_resistant_generator,
        clock=lambda: NOW,
        stop_cleanup_timeout_seconds=0,
    )

    background.tick(NOW)
    await generator_started.wait()
    try:
        await background.stop()
        await cancellation_suppressed.wait()
        with database.connection() as connection:
            before_release = connection.execute(
                """
                SELECT status, usage_reported, input_tokens, output_tokens
                FROM summary_generation_attempts
                """
            ).fetchone()
        assert before_release == ("interrupted", 0, 0, 0)

        release_generator.set()
        await background.drain()

        with database.connection() as connection:
            after_release = connection.execute(
                """
                SELECT status, usage_reported, input_tokens, output_tokens
                FROM summary_generation_attempts
                """
            ).fetchone()
            summary_count = connection.execute(
                "SELECT COUNT(*) FROM summaries"
            ).fetchone()[0]
        assert after_release == ("interrupted", 1, 222, 66)
        assert summary_count == 0
        assert background.last_summarized_message_id(visitor_a) is None
    finally:
        release_generator.set()
        await background.stop()
        await background.drain()


@pytest.mark.anyio
async def test_late_generator_exception_keeps_timeout_usage_unknown(
    database, prompt_builder, repositories, visitors, caplog
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    generator_started = asyncio.Event()
    waiter = TimeoutAfterGeneratorStarts(generator_started)
    cancellation_suppressed = asyncio.Event()
    release_generator = asyncio.Event()

    async def late_failure(job, prompt):
        del job, prompt
        generator_started.set()
        while not release_generator.is_set():
            try:
                await release_generator.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()
        raise RuntimeError("private generated content must not be logged")

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=late_failure,
        clock=lambda: NOW,
        task_waiter=waiter,
    )

    background.tick(NOW)
    try:
        await cancellation_suppressed.wait()
    finally:
        release_generator.set()
        await background.drain()

    with database.connection() as connection:
        attempt = connection.execute(
            """
            SELECT status, usage_reported, input_tokens, output_tokens
            FROM summary_generation_attempts
            """
        ).fetchone()
    assert attempt == ("timed_out", 0, 0, 0)
    assert "private generated content" not in caplog.text


@pytest.mark.anyio
async def test_stop_is_bounded_but_stubborn_summary_keeps_chat_queued(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, visitor_b = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    quota = QuotaService(database)
    adapter = ControlledChatAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    generator_started = asyncio.Event()
    waiter = TimeoutAfterGeneratorStarts(generator_started)
    cancellation_suppressed = asyncio.Event()
    release_generator = asyncio.Event()

    async def cancellation_resistant_generator(job, prompt):
        del job, prompt
        generator_started.set()
        while not release_generator.is_set():
            try:
                await release_generator.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()
        return ("不会保存的结果", {})

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=cancellation_resistant_generator,
        clock=lambda: NOW,
        task_waiter=waiter,
        stop_cleanup_timeout_seconds=0,
    )

    background.tick(NOW)
    try:
        await cancellation_suppressed.wait()
        stop_task = asyncio.create_task(background.stop())
        await stop_task

        assert waiter.timeouts == [120, 0]
        chat = await scheduler.submit(_chat_request(quota, visitor_b, 1))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.started.wait(), timeout=0.05)

        release_generator.set()
        await background.drain()
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        adapter.release.set()
        assert (await asyncio.wait_for(chat.final(), timeout=1)).state == "completed"
    finally:
        release_generator.set()
        adapter.release.set()
        await background.drain()
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_start_recovers_abandoned_running_summary_with_retry_backoff(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    original = _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    first_process = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        clock=lambda: NOW,
    )
    job = first_process.enqueue_due_summaries(NOW)[0]
    attempt_id = first_process.repository.start_summary_attempt(
        job.id, job.visitor_id, NOW - timedelta(minutes=5)
    )

    restarted = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        clock=lambda: NOW,
    )
    await restarted.start()

    with database.connection() as connection:
        status, attempts, retry_at = connection.execute(
            """
            SELECT status, attempt_count, next_retry_at
            FROM summary_jobs WHERE id = ?
            """,
            (job.id,),
        ).fetchone()
    assert (status, attempts) == ("failed", 1)
    assert datetime.fromisoformat(retry_at) == NOW + timedelta(minutes=1)
    with database.connection() as connection:
        attempt_status, finished_at, failure_reason = connection.execute(
            """
            SELECT status, finished_at, failure_reason
            FROM summary_generation_attempts WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
    assert attempt_status == "interrupted"
    assert finished_at == NOW.isoformat()
    assert failure_reason == "coordinator_restart"
    assert restarted.last_summarized_message_id(visitor_a) is None
    assert restarted.enqueue_due_summaries(
        NOW + timedelta(seconds=59)
    ) == []
    retry = restarted.enqueue_due_summaries(NOW + timedelta(minutes=1))[0]
    assert retry.id == job.id
    assert retry.first_message_id == original[0].id
    assert retry.last_message_id == original[9].id
    await restarted.stop()


@pytest.mark.anyio
async def test_start_recovers_one_backfilled_legacy_running_attempt(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    original = _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO summary_jobs
                (id, visitor_id, first_message_id, last_message_id, status,
                 input_tokens, output_tokens, created_at, started_at)
            VALUES ('legacy-running-job', ?, ?, ?, 'running', 55, 12, ?, ?)
            """,
            (
                visitor_a,
                original[0].id,
                original[-1].id,
                (NOW - timedelta(minutes=6)).isoformat(),
                (NOW - timedelta(minutes=5)).isoformat(),
            ),
        )
    database.initialize()
    restarted = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        clock=lambda: NOW,
    )

    await restarted.start()
    try:
        with database.connection() as connection:
            attempts = connection.execute(
                """
                SELECT id, status, failure_reason, usage_reported,
                       input_tokens, output_tokens
                FROM summary_generation_attempts
                WHERE summary_job_id = 'legacy-running-job'
                """
            ).fetchall()
            job_state = connection.execute(
                """
                SELECT status, attempt_count FROM summary_jobs
                WHERE id = 'legacy-running-job'
                """
            ).fetchone()
        assert attempts == [
            (
                "legacy-summary-attempt:legacy-running-job",
                "interrupted",
                "coordinator_restart",
                1,
                55,
                12,
            )
        ]
        assert job_state == ("failed", 1)
        assert restarted.last_summarized_message_id(visitor_a) is None
    finally:
        await restarted.stop()


@pytest.mark.anyio
async def test_each_real_summary_retry_creates_a_separate_attempt(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    scheduler = RecordingSummaryScheduler()
    current = [NOW]
    calls = 0

    async def generate(job, prompt):
        nonlocal calls
        del job, prompt
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary summary failure")
        return SAFE_HAN_SUMMARY, {"input_tokens": 320, "output_tokens": 90}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=scheduler,
        summary_generator=generate,
        clock=lambda: current[0],
    )

    job = background.tick(current[0]).summary_jobs[0]
    await background.drain()
    current[0] += timedelta(minutes=1)
    retry = background.tick(current[0]).summary_jobs[0]
    await background.drain()

    assert retry.id == job.id
    with database.connection() as connection:
        attempts = connection.execute(
            """
            SELECT status, usage_reported, input_tokens, output_tokens,
                   failure_reason
            FROM summary_generation_attempts
            ORDER BY started_at, rowid
            """
        ).fetchall()
    assert attempts == [
        ("failed", 0, 0, 0, "RuntimeError"),
        ("completed", 1, 320, 90, None),
    ]


@pytest.mark.anyio
async def test_invalid_generated_summary_fails_attempt_after_model_usage_is_recorded(
    database, prompt_builder, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )

    async def generate_invalid(job, prompt):
        del job, prompt
        return "太短", {"input_tokens": 123, "output_tokens": 4}

    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        scheduler=RecordingSummaryScheduler(),
        summary_generator=generate_invalid,
        clock=lambda: NOW,
    )

    job = background.tick(NOW).summary_jobs[0]
    await background.drain()

    with database.connection() as connection:
        attempt = connection.execute(
            """
            SELECT status, failure_reason, usage_reported,
                   input_tokens, output_tokens
            FROM summary_generation_attempts
            """
        ).fetchone()
        job_state = connection.execute(
            "SELECT status, attempt_count FROM summary_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
        summary_count = connection.execute(
            "SELECT COUNT(*) FROM summaries"
        ).fetchone()[0]
    assert attempt == ("failed", "invalid_summary", 1, 123, 4)
    assert job_state == ("failed", 1)
    assert summary_count == 0
    assert background.last_summarized_message_id(visitor_a) is None


def test_success_redacts_sensitive_data_and_emits_one_local_notification(
    background, database, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    unsafe = (
        "访客主要分享了近期生活节奏、工作安排和一个准备继续推进的小计划，也说明下次愿意接着交流实际进展。"
        "接待过程保持简短客观，没有形成结论。联系电话13800138000，密码:abc123，"
        "住址北京市朝阳区建国路88号101室。"
        "摘要仅记录对话中明确出现的事项，后续仍以访客主动补充的新信息为准。"
    )

    summary = background.complete_summary(job, unsafe, usage={}, now=NOW)

    assert 100 <= len(summary.text) <= 150
    assert "13800138000" not in summary.text
    assert "abc123" not in summary.text
    assert "建国路88号101室" not in summary.text
    assert "[已隐去]" in summary.text
    with database.connection() as conn:
        notifications = conn.execute(
            "SELECT kind, visitor_id FROM notification_events"
        ).fetchall()
    assert notifications == [("summary_ready", visitor_a)]


def test_summary_rejects_a_long_verbatim_quote_before_persistence(
    background, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    quoted = (
        "访客谈到一件仍待继续的话题，但生成结果不应复制长段原文。"
        "“" + "原话片段" * 18 + "”"
        "后续只需保留客观主题和明确计划，不对访客作额外推断。"
    )

    with pytest.raises(InvalidSummary, match="verbatim"):
        background.complete_summary(job, quoted, usage={}, now=NOW)

    assert background.last_summarized_message_id(visitor_a) is None


def test_summary_rejects_chat_control_marker_and_persists_retry_backoff(
    background, database, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    marked = (
        "访客分享了近期的生活安排，也提到正在处理的一件小事，希望之后继续聊聊进展。"
        "交流围绕当下近况、实际计划和仍待展开的话题，接待人给予了简短回应。"
        "<<LOUNGE_ACTION:suspend>>"
    )

    with pytest.raises(InvalidSummary, match="control marker"):
        background.complete_summary(job, marked, usage={}, now=NOW)

    assert background.last_summarized_message_id(visitor_a) is None
    with database.connection() as connection:
        status, attempts, retry_at = connection.execute(
            """
            SELECT status, attempt_count, next_retry_at
            FROM summary_jobs WHERE id = ?
            """,
            (job.id,),
        ).fetchone()
    assert (status, attempts) == ("failed", 1)
    assert datetime.fromisoformat(retry_at) == NOW + timedelta(minutes=1)


def test_summary_rejects_all_english_output_and_keeps_job_retryable(
    background, database, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    english = (
        "The visitor discussed current plans, recent routines, practical next "
        "steps, and one unfinished topic to revisit during a later conversation."
    )
    assert 100 <= len(english) <= 150

    with pytest.raises(InvalidSummary, match="Han"):
        background.complete_summary(job, english, usage={}, now=NOW)

    with database.connection() as connection:
        status, attempts = connection.execute(
            "SELECT status, attempt_count FROM summary_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    assert (status, attempts) == ("failed", 1)


def test_summary_requires_at_least_one_hundred_han_characters() -> None:
    mixed = (
        "访客提到近期安排和后续计划并希望以后继续交流具体进展" * 3
        + " practical next steps remain available for a later conversation"
    )
    assert 100 <= len(mixed) <= 150
    assert 60 <= len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", mixed)) < 100

    with pytest.raises(InvalidSummary, match="Han"):
        sanitize_summary(mixed)


def test_summary_rejects_excess_non_han_padding_after_han_minimum() -> None:
    padded = SAFE_HAN_SUMMARY + "A" * 31
    assert len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", padded)) == 104

    with pytest.raises(InvalidSummary, match="non-Han"):
        sanitize_summary(padded)


def test_summary_allows_limited_punctuation_around_valid_han_text() -> None:
    punctuated = SAFE_HAN_SUMMARY + "，。；，。；，。；，"

    safe = sanitize_summary(punctuated)

    assert len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", safe)) == 104
    assert len(safe) - 104 == 10


@pytest.mark.parametrize(
    "reviewer_example",
    ["显得焦虑", "属于回避型人格", "关系稳定", "患有抑郁症"],
)
def test_summary_rejects_reviewer_inference_examples(
    background, database, repositories, visitors, reviewer_example
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    inferred = (
        "访客分享了近期生活安排、工作计划和一个希望之后继续讨论的话题。"
        f"生成内容进一步断言访客{reviewer_example}，但这不是对话中可以客观确认的事项。"
        "粗略摘要只应记录明确表达的近况和待续内容，不应据此评价健康、心理、性格或关系。"
    )
    assert 100 <= len(inferred) <= 150

    with pytest.raises(InvalidSummary, match="inference"):
        background.complete_summary(job, inferred, usage={}, now=NOW)

    with database.connection() as connection:
        status, attempts = connection.execute(
            "SELECT status, attempt_count FROM summary_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    assert (status, attempts) == ("failed", 1)


@pytest.mark.parametrize(
    "inference",
    ["有焦虑症", "看起来很焦虑", "性格内向", "两人是恋人", "关系很稳定"],
)
def test_summary_rejects_category_based_unattributed_inference(inference) -> None:
    with pytest.raises(InvalidSummary, match="inference"):
        sanitize_summary(SAFE_HAN_SUMMARY + inference)


@pytest.mark.parametrize(
    "inference",
    ["访客焦虑", "访客患抑郁症", "其人格偏执", "双方为夫妻"],
)
def test_summary_rejects_direct_subject_category_assertions(inference) -> None:
    with pytest.raises(InvalidSummary, match="inference"):
        sanitize_summary(SAFE_HAN_SUMMARY + inference)


@pytest.mark.parametrize(
    "attributed_fact",
    [
        "访客表示自己最近感到焦虑",
        "对方称自己性格较为内向",
        "双方提到彼此是夫妻",
    ],
)
def test_summary_allows_explicitly_attributed_self_reports(attributed_fact) -> None:
    summary = SAFE_HAN_SUMMARY + attributed_fact

    assert sanitize_summary(summary) == summary


@pytest.mark.parametrize(
    "inference",
    [
        "访客很悲伤",
        "对方情绪十分低落",
        "其人格自恋",
        "她属于自恋型人格",
        "双方是朋友",
        "两人关系和睦",
        "访客表示接待人患有抑郁症",
        "访客表示自己认为接待人患有抑郁症",
        "访客表示自己认为小王患有抑郁症",
        "访客表示自己觉得邻居小李很悲伤",
        "访客表示自己认为远房邻居家的小李很悲伤",
        "访客表示自己很悲伤。接待人患有抑郁症",
    ],
)
def test_summary_clause_gate_rejects_unattributed_category_assertions(
    inference,
) -> None:
    with pytest.raises(InvalidSummary, match="inference"):
        sanitize_summary(SAFE_HAN_SUMMARY + inference)


@pytest.mark.parametrize(
    "self_report",
    [
        "访客表示自己很悲伤",
        "对方称本人性格有些自恋",
        "双方提到彼此是朋友",
        "她自述自己最近情绪低落",
        "访客表示自己目前感觉非常焦虑",
        "访客称本人患有焦虑症",
        "双方表示彼此关系比较稳定",
        "对方说自己现在性格比较内向",
    ],
)
def test_summary_clause_gate_allows_reflexive_attributed_reports(
    self_report,
) -> None:
    summary = SAFE_HAN_SUMMARY + self_report

    assert sanitize_summary(summary) == summary


@pytest.mark.parametrize(
    ("unsafe_fragment", "secret"),
    [
        ("密码 hunter2", "hunter2"),
        ("QQ号123456789", "123456789"),
        ("身份证130503670401001", "130503670401001"),
        ("身份证11010519491231002X", "11010519491231002X"),
        ("护照E12345678", "E12345678"),
        ("手机138-0013-8000", "138-0013-8000"),
        ("座机010-87654321", "010-87654321"),
        ("邮箱test@example.com", "test@example.com"),
        ("联系方式wx_user88", "wx_user88"),
        ("地址朝阳区建国路88号101室", "朝阳区建国路88号101室"),
        ("座机(010)87654321", "(010)87654321"),
        (
            "地址河北省石家庄市正定县南楼乡东里双村幸福路12号3室",
            "河北省石家庄市正定县南楼乡东里双村幸福路12号3室",
        ),
        ("地址建国路88号", "建国路88号"),
    ],
)
def test_summary_redacts_each_sensitive_data_category(
    unsafe_fragment, secret
) -> None:
    safe = sanitize_summary(SAFE_HAN_SUMMARY + "；" + unsafe_fragment + "。")

    assert secret not in safe
    assert "[已隐去]" in safe


def test_summary_redacts_literal_reviewer_sensitive_examples() -> None:
    unsafe = (
        "访客分享了近期生活安排和待续计划。密码是 hunter2；手机138-0013-8000；"
        "座机010-87654321；身份证11010519491231002X；护照E12345678；"
        "邮箱test@example.com；微信abc12345；住址北京市朝阳区建国路88号1单元101室。"
        "摘要还应保留足够的客观中文内容，说明交流围绕近况、计划和后续话题展开，"
        "除此之外不作任何额外推断，并等待访客下次主动补充进展。"
    )

    safe = sanitize_summary(unsafe, source_messages=[])

    assert 100 <= len(safe) <= 150
    assert safe.count("[已隐去]") >= 8
    assert all(
        secret not in safe
        for secret in (
            "hunter2",
            "138-0013-8000",
            "010-87654321",
            "11010519491231002X",
            "E12345678",
            "test@example.com",
            "abc12345",
            "建国路88号1单元101室",
        )
    )


@pytest.mark.parametrize(
    ("hierarchical_address", "leading_locality"),
    [
        ("河北省石家庄市平山县西柏坡镇梁家沟村12号", "河北省"),
        ("四川省成都市郫都区唐昌镇战旗村8组16号", "四川省"),
        ("平山县西柏坡镇梁家沟村12号", "平山县"),
        ("西柏坡镇梁家沟村12号", "西柏坡镇"),
        ("黑龙江省哈尔滨市阿城区红星乡新民屯5号", "黑龙江省"),
    ],
)
def test_summary_redacts_hierarchy_only_addresses_without_road_name(
    hierarchical_address, leading_locality
) -> None:
    safe = sanitize_summary(
        SAFE_HAN_SUMMARY + "；地址" + hierarchical_address + "。"
    )

    assert hierarchical_address not in safe
    assert leading_locality not in safe
    assert "[已隐去]" in safe


def test_summary_rejects_raw_output_over_512_characters_before_processing() -> None:
    oversized = "市区村" * 171
    assert len(oversized) == 513

    with pytest.raises(InvalidSummary, match="raw.*512"):
        sanitize_summary(oversized)


@pytest.mark.parametrize("length", [150, 512])
def test_repeated_admin_suffixes_without_house_number_return_quickly(
    length,
) -> None:
    payload = ("市区村" * 171)[:length]
    durations = []
    result = None

    for _ in range(3):
        started = time.perf_counter()
        try:
            result = sanitize_summary(payload)
        except InvalidSummary:
            pass
        durations.append(time.perf_counter() - started)

    assert sorted(durations)[1] < 0.5
    if length == 150:
        assert result == payload


def test_repeated_suffix_characters_do_not_fake_address_hierarchy() -> None:
    summary = SAFE_HAN_SUMMARY + "；地址市区村市区村12号。"

    safe = sanitize_summary(summary)

    assert "地址市区村市区村12号" in safe
    assert "[已隐去]" not in safe


def test_summary_rejects_thirty_character_source_replay_without_quotes(
    background, database, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    copied = (
        "我最近每天早上七点起床，先整理房间，再准备早餐，然后记录当天计划，"
        "晚上复盘完成情况。"
    )
    first = messages.append(
        visitor_a,
        "visitor",
        copied,
        created_at=NOW - timedelta(minutes=40),
    )
    rest = _add_messages(
        messages,
        visitor_a,
        visitor_count=9,
        started_at=NOW - timedelta(minutes=39),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    replayed = (
        "访客说明了自己的日常安排，随后直接重复了这段内容："
        + copied
        + "接待人只需保留主题与后续计划，不应逐字保存如此长的原话片段。"
        "后续摘要也应继续遵守这一限制。"
    )
    assert 100 <= len(replayed) <= 150

    with pytest.raises(InvalidSummary, match="source replay"):
        background.complete_summary(job, replayed, usage={}, now=NOW)

    assert job.first_message_id == first.id
    assert job.last_message_id == rest[-1].id
    with database.connection() as connection:
        status, attempts = connection.execute(
            "SELECT status, attempt_count FROM summary_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
    assert (status, attempts) == ("failed", 1)


def test_summary_rejects_thirty_character_replay_spanning_source_messages() -> None:
    first = "每天早上七点起床后先整理房间并准备早餐"
    second = "随后记录当天计划晚上再复盘完成情况"
    assert len(first) < 30
    assert len(second) < 30
    assert len(first + second) >= 30
    source = [
        Message("m1", "visitor", "visitor", first, NOW),
        Message("m2", "visitor", "visitor", second, NOW),
    ]

    with pytest.raises(InvalidSummary, match="source replay"):
        sanitize_summary(
            SAFE_HAN_SUMMARY + first + second,
            source_messages=source,
        )


def test_summary_allows_short_topical_overlap_with_source(
    background, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    messages.append(
        visitor_a,
        "visitor",
        "最近在准备一次旅行，也在整理工作安排。",
        created_at=NOW - timedelta(minutes=40),
    )
    _add_messages(
        messages,
        visitor_a,
        visitor_count=9,
        started_at=NOW - timedelta(minutes=39),
    )
    job = background.enqueue_due_summaries(NOW)[0]
    topical = (
        "访客主要分享了近期的生活安排，其中包括准备一次旅行，也提到工作事项需要继续整理。"
        "交流还涉及接下来几天的实际计划和一个尚未展开的话题，接待人给予简短回应。"
        "摘要仅保留这些明确主题，后续可以根据访客主动补充的新进展自然续接。"
    )

    summary = background.complete_summary(job, topical, usage={}, now=NOW)

    assert "准备一次旅行" in summary.text
    assert background.last_summarized_message_id(visitor_a) == job.last_message_id


def test_failure_keeps_cursor_and_snapshot_with_persisted_bounded_backoff(
    background, repositories, visitors
) -> None:
    _, messages = repositories
    visitor_a, _ = visitors
    original = _add_messages(
        messages,
        visitor_a,
        visitor_count=10,
        started_at=NOW - timedelta(minutes=40),
    )
    job = background.enqueue_due_summaries(NOW)[0]

    with pytest.raises(InvalidSummary):
        background.complete_summary(job, "太短", usage={}, now=NOW)
    retry_at = NOW + timedelta(minutes=1)

    assert background.last_summarized_message_id(visitor_a) is None
    assert retry_at == NOW + timedelta(minutes=1)
    assert background.enqueue_due_summaries(retry_at - timedelta(seconds=1)) == []
    retry = background.enqueue_due_summaries(retry_at)[0]
    assert retry.id == job.id
    assert retry.first_message_id == original[0].id
    assert retry.last_message_id == original[9].id

    current = retry_at
    for _ in range(4):
        next_retry = background.fail_summary(job, current)
        if next_retry is not None:
            assert timedelta(0) < next_retry - current <= timedelta(hours=1)
            current = next_retry
    assert background.fail_summary(job, current) is None
    assert background.enqueue_due_summaries(current + timedelta(days=1)) == []


@pytest.mark.anyio
async def test_start_ticks_immediately_and_stop_is_idempotent(
    database, prompt_builder, repositories, visitors
) -> None:
    visitor_repository, _ = repositories
    visitor_a, _ = visitors
    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        clock=lambda: NOW,
        interval_seconds=30,
    )
    background.record_activity(visitor_a, NOW - timedelta(minutes=11))

    await background.start()
    await background.start()
    await asyncio.sleep(0)

    assert visitor_repository.visitor(visitor_a).status == "suspended"
    await background.stop()
    await background.stop()
    assert background.running is False


@pytest.mark.anyio
async def test_periodic_run_logs_one_tick_failure_and_continues_scanning(
    database, prompt_builder, repositories, caplog
) -> None:
    del repositories
    background = BackgroundCoordinator(
        database=database,
        prompt_builder=prompt_builder,
        clock=lambda: NOW,
        interval_seconds=0.001,
    )
    original_suspend = background.suspend_idle_visits
    second_scan = asyncio.Event()
    calls = 0

    def flaky_suspend(now):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private visitor text must not be logged")
        second_scan.set()
        return original_suspend(now)

    background.suspend_idle_visits = flaky_suspend
    with caplog.at_level(logging.ERROR, logger="visitor_lounge.background"):
        await background.start()
        try:
            await asyncio.wait_for(second_scan.wait(), timeout=1)
        finally:
            await background.stop()

    assert calls >= 2
    assert "visitor lounge background tick failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private visitor text" not in caplog.text
