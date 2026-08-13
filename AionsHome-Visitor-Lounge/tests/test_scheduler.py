import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from visitor_lounge.models import GenerationChunk, GenerationRequest
from visitor_lounge.quota import QuotaService
from visitor_lounge.repository import (
    MessageRepository,
    RuntimeStateRepository,
    VisitorRepository,
)
from visitor_lounge.scheduler import (
    GIBIBYTE,
    GenerationScheduler,
    QueueFull,
    ResourceGate,
    ResourceSample,
    SchedulerShuttingDown,
    VisitorAlreadyQueued,
)


NOW = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class SchedulerSettings:
    max_waiting: int = 3
    queue_timeout_seconds: int = 120
    generation_timeout_seconds: int = 30


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class ControlledAdapter:
    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.releases: dict[str, asyncio.Event] = {}

    async def generate(self, request: GenerationRequest):
        release = self.releases.setdefault(request.job_id, asyncio.Event())
        await self.started.put(request.visitor_id)
        await release.wait()
        yield GenerationChunk(kind="completed")

    def release(self, job_id: str) -> None:
        self.releases[job_id].set()


class PausedGate:
    def can_start(self) -> bool:
        return False


class SwitchableGate:
    def __init__(self) -> None:
        self.checked = asyncio.Event()
        self.open = False

    def can_start(self) -> bool:
        self.checked.set()
        return self.open


@pytest.mark.anyio
async def test_scheduler_persists_the_actual_resource_gate_state(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    gate = SwitchableGate()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
        resource_gate=gate,
        clock=FakeClock(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 99)
        ticket = await scheduler.submit(request)
        await asyncio.wait_for(gate.checked.wait(), timeout=1)
        persisted = RuntimeStateRepository(database).resource_gate()
        assert persisted is not None
        assert persisted.can_start is False
        assert persisted.checked_at == NOW

        gate.open = True
        assert await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[0]
        persisted = RuntimeStateRepository(database).resource_gate()
        assert persisted is not None
        assert persisted.can_start is True
        adapter.release(request.job_id)
        assert (await asyncio.wait_for(ticket.final(), timeout=1)).state == "completed"
    finally:
        await scheduler.shutdown()


class ParkedWorkerScheduler(GenerationScheduler):
    """Keep submitted live work waiting so priority can be tested deterministically."""

    async def _worker(self) -> None:
        await asyncio.Event().wait()


class SuccessfulAdapter:
    def __init__(self, database) -> None:
        self.database = database
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: GenerationRequest):
        with self.database.transaction(immediate=True) as conn:
            assert conn.execute(
                "SELECT status FROM generation_jobs WHERE id = ?", (request.job_id,)
            ).fetchone()[0] == "running"
        self.entered.set()
        await self.release.wait()
        yield GenerationChunk(kind="text", text="hel")
        yield GenerationChunk(kind="text", text="lo")
        yield GenerationChunk(kind="completed")


class FailingAdapter:
    def __init__(self, visible_text: str) -> None:
        self.visible_text = visible_text

    async def generate(self, request: GenerationRequest):
        del request
        if self.visible_text:
            yield GenerationChunk(kind="text", text=self.visible_text)
        raise RuntimeError("adapter failed")


class ReportedZeroUsageAdapter:
    async def generate(self, request: GenerationRequest):
        del request
        yield GenerationChunk(
            kind="usage", usage={"input_tokens": 0, "output_tokens": 0}
        )
        yield GenerationChunk(kind="completed")


class InvalidUsageAdapter:
    async def generate(self, request: GenerationRequest):
        del request
        yield GenerationChunk(
            kind="usage", usage={"input_tokens": -1, "output_tokens": 0}
        )
        yield GenerationChunk(kind="completed")


class ClosingAdapter:
    async def generate(self, request: GenerationRequest):
        del request
        yield GenerationChunk(kind="text", text="先聊到这里吧。")
        yield GenerationChunk(kind="completed", action="closing")


class ConfirmFailsOnce:
    def __init__(self, quota: QuotaService) -> None:
        self.quota = quota
        self.failed = False

    def confirm(self, request_id: str):
        state = self.quota.confirm(request_id)
        if not self.failed:
            self.failed = True
            raise RuntimeError("confirm callback failed after commit")
        return state

    def refund_once(self, request_id: str, reason: str):
        return self.quota.refund_once(request_id, reason)


class ExpiryRefundRaisesAfterCommit:
    def __init__(self, quota: QuotaService) -> None:
        self.quota = quota
        self.raised = False

    def confirm(self, request_id: str):
        return self.quota.confirm(request_id)

    def refund_once(self, request_id: str, reason: str):
        state = self.quota.refund_once(request_id, reason)
        if reason == "queue_timeout" and not self.raised:
            self.raised = True
            raise RuntimeError("refund callback failed after commit")
        return state


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_request(quota, visitor_id: str, number: int) -> GenerationRequest:
    request_id = f"request-{number}"
    reservation = quota.reserve(visitor_id, request_id, NOW)
    return GenerationRequest(
        job_id=reservation.job_id,
        request_id=request_id,
        visitor_id=visitor_id,
        message_id=f"message-{number}",
        prompt=f"prompt-{number}",
    )


@pytest.fixture
def visitors(database):
    database.initialize()
    repository = VisitorRepository(database)
    return [repository.create_unclaimed_visitor() for _ in range(5)]


@pytest.fixture
def quota(database, visitors):
    del visitors
    return QuotaService(database)


@pytest.mark.anyio
async def test_fifo_has_one_running_slot_and_three_waiting_slots(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        requests = [
            make_request(quota, visitor, index)
            for index, visitor in enumerate(visitors)
        ]
        first = await scheduler.submit(requests[0])
        assert await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[0]

        waiting = [await scheduler.submit(request) for request in requests[1:4]]
        assert first.position == 0
        assert [ticket.position for ticket in waiting] == [1, 2, 3]
        with pytest.raises(QueueFull):
            await scheduler.submit(requests[4])

        tickets = [first, *waiting]
        for index, (request, ticket) in enumerate(zip(requests[:4], tickets)):
            adapter.release(request.job_id)
            assert (
                await asyncio.wait_for(ticket.final(), timeout=1)
            ).state == "completed"
            if index < 3:
                assert (
                    await asyncio.wait_for(adapter.started.get(), timeout=1)
                    == visitors[index + 1]
                )
                assert [queued.position for queued in tickets[index + 1 :]] == [
                    0,
                    *range(1, 4 - index - 1),
                ]
        assert scheduler.started_visitor_ids == visitors[:4]
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_live_chat_waiting_or_running_prevents_low_priority_lease(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    work_started = False

    async def summary_work() -> None:
        nonlocal work_started
        work_started = True

    try:
        request = make_request(quota, visitors[0], 800)
        ticket = await scheduler.submit(request)
        assert await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[0]

        acquired = await scheduler.run_low_priority(summary_work)

        assert acquired is False
        assert work_started is False
        adapter.release(request.job_id)
        assert (await asyncio.wait_for(ticket.final(), timeout=1)).state == "completed"
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_waiting_live_chat_prevents_low_priority_lease_before_any_start(
    database, quota, visitors
):
    scheduler = ParkedWorkerScheduler(
        database=database,
        quota=quota,
        adapter=ControlledAdapter(),
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    work_started = False

    async def summary_work() -> None:
        nonlocal work_started
        work_started = True

    try:
        ticket = await scheduler.submit(make_request(quota, visitors[0], 802))
        assert ticket.position == 1

        acquired = await scheduler.run_low_priority(summary_work)

        assert acquired is False
        assert work_started is False
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_low_priority_lease_atomically_holds_slot_while_new_chat_waits(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    summary_started = asyncio.Event()
    release_summary = asyncio.Event()

    async def summary_work() -> None:
        summary_started.set()
        await release_summary.wait()

    try:
        summary_task = asyncio.create_task(scheduler.run_low_priority(summary_work))
        await asyncio.wait_for(summary_started.wait(), timeout=1)

        request = make_request(quota, visitors[0], 801)
        ticket = await scheduler.submit(request)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.started.get(), timeout=0.05)

        release_summary.set()
        assert await asyncio.wait_for(summary_task, timeout=1) is True
        assert await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[0]
        adapter.release(request.job_id)
        assert (await asyncio.wait_for(ticket.final(), timeout=1)).state == "completed"
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_same_visitor_cannot_hold_two_queued_or_running_jobs(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 0)
        await scheduler.submit(request)
        await asyncio.wait_for(adapter.started.get(), timeout=1)

        with pytest.raises(VisitorAlreadyQueued):
            await scheduler.submit(
                replace(request, request_id="another", job_id="another")
            )
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_queue_timeout_refunds_reservation_before_start(
    database, quota, visitors
):
    clock = FakeClock()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=ControlledAdapter(),
        settings=SchedulerSettings(),
        resource_gate=PausedGate(),
        clock=clock,
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 0)
        ticket = await scheduler.submit(request)
        clock.advance(seconds=120)
        await scheduler.expire_waiters()

        assert (await ticket.final()).state == "queue_timeout"
        assert quota.state(request.visitor_id).reserved == 0
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_worker_expires_head_at_timeout_before_starting_next_visitor(
    database, quota, visitors
):
    clock = FakeClock()
    gate = SwitchableGate()
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
        resource_gate=gate,
        clock=clock,
        poll_interval=10,
    )
    await scheduler.start()
    try:
        expired_request = make_request(quota, visitors[0], 0)
        expired = await scheduler.submit(expired_request)
        await asyncio.wait_for(gate.checked.wait(), timeout=1)

        clock.advance(seconds=120)
        gate.open = True
        next_request = make_request(quota, visitors[1], 1)
        next_ticket = await scheduler.submit(next_request)

        assert (
            await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[1]
        )
        assert (
            await asyncio.wait_for(expired.final(), timeout=1)
        ).state == "queue_timeout"
        assert quota.state(expired_request.visitor_id).reserved == 0
        adapter.release(next_request.job_id)
        assert (
            await asyncio.wait_for(next_ticket.final(), timeout=1)
        ).state == "completed"
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_expiry_refund_exception_finalizes_ticket_and_worker_runs_next(
    database, quota, visitors
):
    clock = FakeClock()
    gate = SwitchableGate()
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=ExpiryRefundRaisesAfterCommit(quota),
        adapter=adapter,
        settings=SchedulerSettings(),
        resource_gate=gate,
        clock=clock,
        poll_interval=10,
    )
    await scheduler.start()
    try:
        expired_request = make_request(quota, visitors[0], 0)
        expired = await scheduler.submit(expired_request)
        await asyncio.wait_for(gate.checked.wait(), timeout=1)

        clock.advance(seconds=120)
        gate.open = True
        next_request = make_request(quota, visitors[1], 1)
        next_ticket = await scheduler.submit(next_request)

        assert (
            await asyncio.wait_for(expired.final(), timeout=1)
        ).state == "queue_timeout"
        assert quota.state(expired_request.visitor_id).reserved == 0
        with database.connection() as conn:
            expired_status = conn.execute(
                "SELECT status FROM generation_jobs WHERE id = ?",
                (expired_request.job_id,),
            ).fetchone()[0]
        assert expired_status == "cancelled"
        assert (
            await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[1]
        )

        adapter.release(next_request.job_id)
        assert (
            await asyncio.wait_for(next_ticket.final(), timeout=1)
        ).state == "completed"
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_lifecycle_events_and_visible_text_are_persisted_without_adapter_transaction(
    database, quota, visitors
):
    adapter = SuccessfulAdapter(database)
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 0)
        ticket = await scheduler.submit(request)
        await asyncio.wait_for(adapter.entered.wait(), timeout=1)
        with database.connection() as conn:
            running = conn.execute(
                "SELECT status, confirmed_at FROM generation_jobs WHERE id = ?",
                (request.job_id,),
            ).fetchone()
        assert running[0] == "running"
        assert running[1] is not None

        adapter.release.set()
        assert (await asyncio.wait_for(ticket.final(), timeout=1)).visible_text == "hello"
        events = [event async for event in scheduler.events(request.job_id)]

        assert [event.kind for event in events] == [
            "queued",
            "started",
            "text",
            "text",
            "completed",
        ]
        assert events[0].data["position"] == 1
        assert [event.data["text"] for event in events[2:4]] == ["hel", "lo"]
        with database.connection() as conn:
            completed = conn.execute(
                """
                SELECT status, visible_text, started_at, finished_at
                FROM generation_jobs WHERE id = ?
                """,
                (request.job_id,),
            ).fetchone()
        assert completed[:2] == ("completed", "hello")
        assert completed[2] is not None
        assert completed[3] is not None
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_scheduler_preserves_closing_action(database, quota, visitors):
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=ClosingAdapter(),
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 702)
        ticket = await scheduler.submit(request)

        result = await asyncio.wait_for(ticket.final(), timeout=1)

        assert result.state == "completed"
        with database.connection() as conn:
            assert conn.execute(
                "SELECT action FROM generation_jobs WHERE id = ?",
                (request.job_id,),
            ).fetchone() == ("closing",)
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_explicit_zero_usage_is_persisted_as_reported(database, quota, visitors):
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=ReportedZeroUsageAdapter(),
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 701)
        ticket = await scheduler.submit(request)

        assert (await asyncio.wait_for(ticket.final(), timeout=1)).state == "completed"
        with database.connection() as conn:
            call = conn.execute(
                """
                SELECT usage_reported, input_tokens, output_tokens
                FROM model_calls WHERE job_id = ?
                """,
                (request.job_id,),
            ).fetchone()
        assert call == (1, 0, 0)
        job = VisitorRepository(database).job_by_id(request.job_id)
        assert job.usage_reported is True
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_invalid_usage_event_keeps_usage_unknown(database, quota, visitors):
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=InvalidUsageAdapter(),
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 702)
        ticket = await scheduler.submit(request)

        assert (await asyncio.wait_for(ticket.final(), timeout=1)).state == "completed"
        with database.connection() as conn:
            call = conn.execute(
                """
                SELECT usage_reported, input_tokens, output_tokens
                FROM model_calls WHERE job_id = ?
                """,
                (request.job_id,),
            ).fetchone()
        assert call == (0, 0, 0)
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_events_are_replayable_for_two_subscribers_and_from_event_id(
    database, quota, visitors
):
    adapter = SuccessfulAdapter(database)
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 70)
        ticket = await scheduler.submit(request)
        await asyncio.wait_for(adapter.entered.wait(), timeout=1)
        adapter.release.set()
        await asyncio.wait_for(ticket.final(), timeout=1)

        async def collect(after_event_id: int = 0):
            return [
                event
                async for event in scheduler.events(
                    request.job_id, after_event_id=after_event_id
                )
            ]

        first = await asyncio.wait_for(collect(), timeout=1)
        second = await asyncio.wait_for(collect(), timeout=1)
        resumed = await asyncio.wait_for(
            collect(after_event_id=first[2].event_id), timeout=1
        )

        assert [event.kind for event in first] == [event.kind for event in second]
        assert [event.event_id for event in first] == list(range(1, len(first) + 1))
        assert [event.event_id for event in resumed] == [
            event.event_id for event in first[3:]
        ]
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_terminal_ticket_history_is_bounded_and_sensitive_prompt_is_dropped(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
        terminal_ticket_limit=1,
        event_history_limit=3,
    )
    await scheduler.start()
    try:
        requests = [make_request(quota, visitors[index], 80 + index) for index in range(2)]
        tickets = []
        for request in requests:
            ticket = await scheduler.submit(request)
            tickets.append(ticket)
            await asyncio.wait_for(adapter.started.get(), timeout=1)
            adapter.release(request.job_id)
            await asyncio.wait_for(ticket.final(), timeout=1)

        with pytest.raises(KeyError):
            scheduler.ticket(requests[0].job_id)
        assert tickets[0].request is None
        async def collect_retained():
            return [
                event async for event in scheduler.events(requests[1].job_id)
            ]

        retained_events = await asyncio.wait_for(collect_retained(), timeout=1)
        assert len(retained_events) <= 3
        assert retained_events[-1].kind == "completed"
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("visible_text", "expected_used", "expect_refund"),
    [("", 0, True), ("part", 1, False)],
)
async def test_failure_refunds_only_when_no_text_was_visible(
    database, quota, visitors, visible_text, expected_used, expect_refund
):
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=FailingAdapter(visible_text),
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        request = make_request(quota, visitors[0], 0)
        ticket = await scheduler.submit(request)
        result = await asyncio.wait_for(ticket.final(), timeout=1)
        scheduler._finish_after_error(ticket, "failed", "duplicate_callback")

        assert (result.state, result.visible_text) == ("failed", visible_text)
        assert quota.state(request.visitor_id).used == expected_used
        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT status, visible_text, refunded_at
                FROM generation_jobs WHERE id = ?
                """,
                (request.job_id,),
            ).fetchone()
            call = conn.execute(
                """
                SELECT usage_reported, input_tokens, output_tokens
                FROM model_calls WHERE job_id = ?
                """,
                (request.job_id,),
            ).fetchone()
        assert row[:2] == ("failed", visible_text)
        assert (row[2] is not None) is expect_refund
        assert call == (0, 0, 0)
        host_replies = [
            message.content
            for message in MessageRepository(database).recent(request.visitor_id)
            if message.sender == "host"
        ]
        assert host_replies == ([visible_text] if visible_text else [])
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_confirm_exception_fails_one_ticket_and_worker_runs_next_visitor(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=ConfirmFailsOnce(quota),
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    try:
        failed_request = make_request(quota, visitors[0], 0)
        next_request = make_request(quota, visitors[1], 1)
        failed = await scheduler.submit(failed_request)
        next_ticket = await scheduler.submit(next_request)

        assert (await asyncio.wait_for(failed.final(), timeout=1)).state == "failed"
        assert quota.state(failed_request.visitor_id).used == 0
        assert quota.state(failed_request.visitor_id).reserved == 0
        assert (
            await asyncio.wait_for(adapter.started.get(), timeout=1) == visitors[1]
        )

        adapter.release(next_request.job_id)
        assert (
            await asyncio.wait_for(next_ticket.final(), timeout=1)
        ).state == "completed"
        with database.connection() as conn:
            statuses = conn.execute(
                "SELECT status FROM generation_jobs ORDER BY request_id"
            ).fetchall()
        assert statuses == [("failed",), ("completed",)]
    finally:
        await scheduler.shutdown()


@pytest.mark.anyio
async def test_shutdown_rejects_new_jobs_and_cancels_active_and_queued(
    database, quota, visitors
):
    adapter = ControlledAdapter()
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=adapter,
        settings=SchedulerSettings(),
    )
    await scheduler.start()
    active_request = make_request(quota, visitors[0], 0)
    queued_request = make_request(quota, visitors[1], 1)
    active = await scheduler.submit(active_request)
    await asyncio.wait_for(adapter.started.get(), timeout=1)
    queued = await scheduler.submit(queued_request)

    await scheduler.shutdown()

    assert (await active.final()).state == "cancelled"
    assert (await queued.final()).state == "cancelled"
    assert quota.state(active_request.visitor_id).used == 0
    assert quota.state(queued_request.visitor_id).reserved == 0
    with pytest.raises(SchedulerShuttingDown):
        await scheduler.submit(replace(active_request, request_id="late"))
    with database.connection() as conn:
        statuses = conn.execute(
            "SELECT status FROM generation_jobs ORDER BY request_id"
        ).fetchall()
    assert statuses == [("cancelled",), ("cancelled",)]


@pytest.mark.anyio
async def test_shutdown_is_bounded_while_stubborn_low_priority_owner_remains(
    database, quota, visitors
):
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=ControlledAdapter(),
        settings=SchedulerSettings(),
        shutdown_cleanup_timeout_seconds=0,
    )
    await scheduler.start()
    owner_started = asyncio.Event()
    cancellation_suppressed = asyncio.Event()
    release_owner = asyncio.Event()
    second_started = asyncio.Event()

    async def stubborn_owner() -> None:
        owner_started.set()
        while not release_owner.is_set():
            try:
                await release_owner.wait()
            except asyncio.CancelledError:
                cancellation_suppressed.set()

    async def second_generation() -> None:
        second_started.set()

    owner_task = asyncio.create_task(scheduler.run_low_priority(stubborn_owner))
    await owner_started.wait()
    try:
        await scheduler.shutdown()

        assert cancellation_suppressed.is_set()
        assert not owner_task.done()
        with pytest.raises(SchedulerShuttingDown):
            await scheduler.run_low_priority(second_generation)
        assert not second_started.is_set()
        assert owner_task in scheduler._retained_shutdown_tasks

        release_owner.set()
        assert await asyncio.wait_for(owner_task, timeout=1) is True
        await asyncio.sleep(0)
        assert scheduler._retained_shutdown_tasks == set()
    finally:
        release_owner.set()
        await asyncio.gather(owner_task, return_exceptions=True)


def test_resource_gate_requires_continuous_high_cpu_and_blocks_low_memory():
    clock = FakeMonotonic()
    sample = ResourceSample(cpu_percent=85.0, available_memory_bytes=3 * GIBIBYTE)

    def sampler() -> ResourceSample:
        return sample

    gate = ResourceGate(sampler=sampler, clock=clock)
    assert gate.can_start() is True
    clock.advance(29)
    assert gate.can_start() is True
    clock.advance(1)
    assert gate.can_start() is False

    sample = ResourceSample(cpu_percent=0.0, available_memory_bytes=GIBIBYTE)
    assert gate.can_start() is False
    clock.advance(10)
    sample = ResourceSample(cpu_percent=85.0, available_memory_bytes=3 * GIBIBYTE)
    assert gate.can_start() is True


@pytest.mark.anyio
async def test_start_cleans_up_abandoned_persisted_jobs(database, quota, visitors):
    queued = make_request(quota, visitors[0], 0)
    running_without_text = make_request(quota, visitors[1], 1)
    running_with_text = make_request(quota, visitors[2], 2)
    quota.confirm(running_without_text.request_id)
    quota.confirm(running_with_text.request_id)
    with database.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = 'running' WHERE id IN (?, ?)",
            (running_without_text.job_id, running_with_text.job_id),
        )
        conn.execute(
            "UPDATE generation_jobs SET visible_text = 'visible' WHERE id = ?",
            (running_with_text.job_id,),
        )
    scheduler = GenerationScheduler(
        database=database,
        quota=quota,
        adapter=ControlledAdapter(),
        settings=SchedulerSettings(),
    )

    await scheduler.start()
    await scheduler.shutdown()

    assert quota.state(queued.visitor_id).reserved == 0
    assert quota.state(running_without_text.visitor_id).used == 0
    assert quota.state(running_with_text.visitor_id).used == 1
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT status FROM generation_jobs ORDER BY request_id"
        ).fetchall()
    assert rows == [("cancelled",), ("cancelled",), ("cancelled",)]
    assert [
        message.content
        for message in MessageRepository(database).recent(
            running_with_text.visitor_id
        )
        if message.sender == "host"
    ] == ["visible"]
