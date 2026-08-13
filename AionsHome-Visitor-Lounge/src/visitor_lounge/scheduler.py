"""Bounded, fair scheduling for persisted Visitor Lounge generations."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from threading import Lock
import time
from typing import Protocol
from uuid import uuid4

from visitor_lounge.database import Database
from visitor_lounge.models import GenerationChunk, GenerationRequest, JobEvent
from visitor_lounge.quota import QuotaService
from visitor_lounge.repository import RuntimeStateRepository, _insert_message


GIBIBYTE = 1024**3
RESOURCE_STATE_HEARTBEAT = timedelta(seconds=5)
SAFETY_LOCK_DURATION = timedelta(hours=24)
_windows_cpu_lock = Lock()
_windows_cpu_times: tuple[int, int] | None = None


class QueueFull(RuntimeError):
    """Raised when all bounded waiting positions are occupied."""


class VisitorAlreadyQueued(RuntimeError):
    """Raised when a visitor already owns a queued or running job."""


class SchedulerShuttingDown(RuntimeError):
    """Raised when a scheduler no longer accepts submissions."""


class GenerationAdapter(Protocol):
    def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]: ...


class SchedulerConfiguration(Protocol):
    max_waiting: int
    queue_timeout_seconds: int
    generation_timeout_seconds: int


def _validated_usage(usage: Mapping[str, int]) -> dict[str, int] | None:
    reported: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens"):
        if key not in usage:
            continue
        value = usage[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        reported[key] = value
    return reported or None


@dataclass(frozen=True)
class ResourceSample:
    cpu_percent: float
    available_memory_bytes: int


@dataclass(frozen=True)
class QueueResult:
    state: str
    visible_text: str = ""


def _default_resource_sample() -> ResourceSample:
    """Collect a dependency-free, best-effort system resource sample."""
    cpu_percent = 0.0
    try:
        load_one_minute = os.getloadavg()[0]
        cpu_percent = load_one_minute * 100.0 / max(os.cpu_count() or 1, 1)
    except (AttributeError, OSError):
        pass

    available_memory = 2 * GIBIBYTE
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                available_memory = int(status.available_physical)
            cpu_percent = _windows_cpu_percent(ctypes)
        except (AttributeError, OSError):
            pass
    else:
        try:
            available_memory = int(os.sysconf("SC_AVPHYS_PAGES")) * int(
                os.sysconf("SC_PAGE_SIZE")
            )
        except (AttributeError, OSError, ValueError):
            pass
    return ResourceSample(cpu_percent, available_memory)


def _windows_cpu_percent(ctypes_module: object) -> float:
    """Calculate utilization between two dependency-free Windows samples."""
    global _windows_cpu_times

    ctypes = ctypes_module

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        def value(self) -> int:
            return (int(self.high) << 32) | int(self.low)

    idle = FileTime()
    kernel = FileTime()
    user = FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return 0.0
    current = (idle.value(), kernel.value() + user.value())
    with _windows_cpu_lock:
        previous = _windows_cpu_times
        _windows_cpu_times = current
    if previous is None:
        return 0.0
    idle_delta = current[0] - previous[0]
    total_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta))


class ResourceGate:
    """Pause new starts under sustained CPU pressure or low memory."""

    def __init__(
        self,
        sampler: Callable[[], ResourceSample] = _default_resource_sample,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sampler = sampler
        self._clock = clock
        self._high_cpu_since: float | None = None

    def can_start(self) -> bool:
        sample = self._sampler()
        now = self._clock()
        if sample.cpu_percent >= 85.0:
            if self._high_cpu_since is None:
                self._high_cpu_since = now
            cpu_allows_start = now - self._high_cpu_since < 30.0
        else:
            self._high_cpu_since = None
            cpu_allows_start = True
        memory_allows_start = sample.available_memory_bytes >= 2 * GIBIBYTE
        return cpu_allows_start and memory_allows_start


class QueueTicket:
    """Handle for observing one submitted generation."""

    def __init__(
        self,
        request: GenerationRequest,
        queued_at: datetime,
        *,
        event_history_limit: int = 128,
    ) -> None:
        if event_history_limit < 1:
            raise ValueError("event history limit must be positive")
        self.request: GenerationRequest | None = request
        self.job_id = request.job_id
        self.request_id = request.request_id
        self.visitor_id = request.visitor_id
        self.queued_at = queued_at
        self.visible_text = ""
        self.started = False
        self.position: int | None = None
        self._final: asyncio.Future[QueueResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._events: deque[JobEvent] = deque(maxlen=event_history_limit)
        self._next_event_id = 1
        self._changed = asyncio.Event()
        self._terminal = False

    async def final(self) -> QueueResult:
        return await asyncio.shield(self._final)

    def emit(self, event: JobEvent) -> None:
        sequenced = replace(event, event_id=self._next_event_id)
        self._next_event_id += 1
        self._events.append(sequenced)
        if event.kind in {"completed", "failed"}:
            self._terminal = True
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def finish(
        self,
        state: str,
        *,
        action: str = "continue",
        usage: dict[str, int] | None = None,
        usage_reported: bool = False,
    ) -> None:
        if self._final.done():
            return
        terminal_kind = "completed" if state == "completed" else "failed"
        self.position = None
        self.emit(
            JobEvent(
                terminal_kind,
                {
                    "state": state,
                    "action": action,
                    "usage": usage if usage_reported else {},
                    "usage_reported": usage_reported,
                },
            )
        )
        self._final.set_result(QueueResult(state=state, visible_text=self.visible_text))

    async def events(self, *, after_event_id: int = 0) -> AsyncIterator[JobEvent]:
        cursor = max(after_event_id, 0)
        while True:
            available = [event for event in self._events if event.event_id > cursor]
            for event in available:
                cursor = event.event_id
                yield event
            latest = self._next_event_id - 1
            if self._terminal and cursor >= latest:
                return
            changed = self._changed
            await changed.wait()

    @property
    def latest_event_id(self) -> int:
        return self._next_event_id - 1

    def drop_sensitive_request(self) -> None:
        self.request = None


class GenerationScheduler:
    """Run a single FIFO generation worker over a bounded waiting queue."""

    def __init__(
        self,
        *,
        database: Database,
        quota: QuotaService,
        adapter: GenerationAdapter,
        settings: SchedulerConfiguration,
        resource_gate: ResourceGate | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval: float = 0.05,
        terminal_ticket_limit: int = 64,
        event_history_limit: int = 128,
        shutdown_cleanup_timeout_seconds: float = 1,
        safety_template_provider: Callable[[], str] | None = None,
    ) -> None:
        if terminal_ticket_limit < 0:
            raise ValueError("terminal ticket limit must not be negative")
        if shutdown_cleanup_timeout_seconds < 0:
            raise ValueError("shutdown cleanup timeout must not be negative")
        self.database = database
        self.quota = quota
        self.adapter = adapter
        self.settings = settings
        self.resource_gate = resource_gate or ResourceGate()
        self.runtime_state = RuntimeStateRepository(database)
        self._last_resource_can_start: bool | None = None
        self._last_resource_recorded_at: datetime | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval = poll_interval
        self._terminal_ticket_limit = terminal_ticket_limit
        self._event_history_limit = event_history_limit
        self._shutdown_cleanup_timeout_seconds = (
            shutdown_cleanup_timeout_seconds
        )
        self._safety_template_provider = safety_template_provider
        self._condition = asyncio.Condition()
        self._waiting: deque[QueueTicket] = deque()
        self._active_visitors: set[str] = set()
        self._tickets: dict[str, QueueTicket] = {}
        self._terminal_tickets: deque[str] = deque()
        self._tasks: list[asyncio.Task[object]] = []
        self._retained_shutdown_tasks: set[asyncio.Task[object]] = set()
        self._active_ticket: QueueTicket | None = None
        self._low_priority_active = False
        self._low_priority_task: asyncio.Task[object] | None = None
        self._started_visitors: list[str] = []
        self._started = False
        self._shutting_down = False

    @property
    def started_visitor_ids(self) -> list[str]:
        return list(self._started_visitors)

    def queue_position(self, job_id: str) -> int | None:
        ticket = self._tickets.get(job_id)
        return None if ticket is None else ticket.position

    def ticket(self, job_id: str) -> QueueTicket:
        ticket = self._tickets.get(job_id)
        if ticket is None:
            raise KeyError(job_id)
        return ticket

    def evict_ticket(self, job_id: str) -> bool:
        ticket = self._tickets.get(job_id)
        if ticket is None or not ticket._terminal:
            return False
        del self._tickets[job_id]
        try:
            self._terminal_tickets.remove(job_id)
        except ValueError:
            pass
        return True

    async def start(self) -> None:
        async with self._condition:
            if self._started:
                return
            if self._shutting_down:
                raise SchedulerShuttingDown()
            self._started = True
            self._recover_abandoned_jobs()
            self._tasks = [
                asyncio.create_task(self._worker(), name="visitor-lounge-generator"),
                asyncio.create_task(self._expiry_loop(), name="visitor-lounge-expiry"),
            ]

    async def submit(self, request: GenerationRequest) -> QueueTicket:
        async with self._condition:
            if self._shutting_down:
                raise SchedulerShuttingDown()
            if not self._started:
                raise RuntimeError("scheduler has not been started")
            if request.visitor_id in self._active_visitors:
                raise VisitorAlreadyQueued()
            if len(self._waiting) >= self.settings.max_waiting:
                raise QueueFull()
            ticket = QueueTicket(
                request,
                self._clock(),
                event_history_limit=self._event_history_limit,
            )
            self._waiting.append(ticket)
            self._refresh_positions()
            ticket.emit(
                JobEvent(
                    "queued",
                    {"job_id": request.job_id, "position": ticket.position},
                )
            )
            self._active_visitors.add(request.visitor_id)
            self._tickets[request.job_id] = ticket
            self._condition.notify_all()
            return ticket

    async def run_low_priority(
        self, work: Callable[[], Awaitable[None]]
    ) -> bool:
        """Atomically lease the generation slot only when no live chat needs it."""
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("low-priority work requires an asyncio task")
        async with self._condition:
            if self._shutting_down:
                raise SchedulerShuttingDown()
            if not self._started:
                raise RuntimeError("scheduler has not been started")
            if (
                self._active_ticket is not None
                or self._waiting
                or self._low_priority_active
                or not self._resources_allow_start()
            ):
                return False
            self._low_priority_active = True
            self._low_priority_task = current
        try:
            await work()
            return True
        finally:
            async with self._condition:
                self._low_priority_active = False
                if self._low_priority_task is current:
                    self._low_priority_task = None
                self._condition.notify_all()

    async def events(
        self, job_id: str, *, after_event_id: int = 0
    ) -> AsyncIterator[JobEvent]:
        ticket = self._tickets.get(job_id)
        if ticket is None:
            raise KeyError(job_id)
        async for event in ticket.events(after_event_id=after_event_id):
            yield event

    async def expire_waiters(self) -> None:
        async with self._condition:
            expired = self._remove_expired_waiters(self._clock())
        for ticket in expired:
            self._finish_expired(ticket)

    async def shutdown(self) -> None:
        queued: list[QueueTicket] = []
        low_priority_task: asyncio.Task[object] | None = None
        async with self._condition:
            if self._shutting_down:
                tasks = set(self._tasks) | self._retained_shutdown_tasks
            else:
                self._shutting_down = True
                queued = list(self._waiting)
                self._waiting.clear()
                self._refresh_positions()
                for ticket in queued:
                    self._active_visitors.discard(ticket.visitor_id)
                tasks = set(self._tasks) | self._retained_shutdown_tasks
                low_priority_task = self._low_priority_task
                self._condition.notify_all()
        for ticket in queued:
            self._persist_failed_generation(
                ticket,
                state="cancelled",
                refund_reason="shutdown_before_start",
            )
            self._finish_ticket(ticket, "cancelled")
        for task in tasks:
            task.cancel()
        current = asyncio.current_task()
        if low_priority_task is not None and low_priority_task is not current:
            low_priority_task.cancel()
            tasks.add(low_priority_task)
        if tasks:
            newly_retained = tasks - self._retained_shutdown_tasks
            self._retained_shutdown_tasks.update(tasks)
            for task in newly_retained:
                task.add_done_callback(self._shutdown_task_finished)
            await asyncio.wait(
                tasks, timeout=self._shutdown_cleanup_timeout_seconds
            )

    def _shutdown_task_finished(self, task: asyncio.Task[object]) -> None:
        self._retained_shutdown_tasks.discard(task)
        try:
            self._tasks.remove(task)
        except ValueError:
            pass
        if not task.cancelled():
            task.exception()

    async def _worker(self) -> None:
        while True:
            ticket = await self._next_ticket()
            if ticket is None:
                return
            try:
                await self._run_ticket(ticket)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._finish_after_error(ticket, "failed", "worker_error")
                await self._release_ticket(ticket)

    async def _next_ticket(self) -> QueueTicket | None:
        while True:
            expired: list[QueueTicket] = []
            async with self._condition:
                if self._shutting_down:
                    return None
                expired = self._remove_expired_waiters(self._clock())
                if (
                    not expired
                    and self._waiting
                    and not self._low_priority_active
                    and self._resources_allow_start()
                ):
                    ticket = self._waiting.popleft()
                    ticket.started = True
                    ticket.position = 0
                    self._refresh_positions()
                    self._active_ticket = ticket
                    return ticket
                if not expired:
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(), timeout=self._poll_interval
                        )
                    except TimeoutError:
                        pass
            for ticket in expired:
                self._finish_expired(ticket)

    def _resources_allow_start(self) -> bool:
        can_start = self.resource_gate.can_start()
        now = self._clock()
        heartbeat_due = (
            self._last_resource_recorded_at is None
            or now - self._last_resource_recorded_at >= RESOURCE_STATE_HEARTBEAT
        )
        if can_start != self._last_resource_can_start or heartbeat_due:
            try:
                self.runtime_state.record_resource_gate(
                    can_start=can_start, checked_at=now
                )
            except sqlite3.Error:
                pass
            else:
                self._last_resource_can_start = can_start
                self._last_resource_recorded_at = now
        return can_start

    async def _run_ticket(self, ticket: QueueTicket) -> None:
        request = ticket.request
        if request is None:
            raise RuntimeError("active ticket request was already discarded")
        action = "continue"
        usage: dict[str, int] = {}
        usage_reported = False
        try:
            self._set_running(request.job_id)
            self.quota.confirm(request.request_id)
            self._start_model_call(request.job_id, request.visitor_id)
            self._started_visitors.append(request.visitor_id)
            ticket.emit(JobEvent("started", {"job_id": request.job_id}))
            async with asyncio.timeout(self.settings.generation_timeout_seconds):
                async for chunk in self.adapter.generate(request):
                    if chunk.kind == "text" and chunk.text:
                        ticket.visible_text += chunk.text
                        self._append_visible_text(request.job_id, chunk.text)
                        ticket.emit(
                            JobEvent(
                                "text",
                                {
                                    "text": chunk.text,
                                    "visible_text": ticket.visible_text,
                                },
                            )
                        )
                    elif chunk.kind == "usage":
                        reported_usage = _validated_usage(chunk.usage)
                        if reported_usage is not None:
                            usage = reported_usage
                            usage_reported = True
                            self._persist_usage(request.job_id, usage)
                            ticket.emit(JobEvent("usage", usage))
                    elif chunk.kind == "completed":
                        action = (
                            chunk.action
                            if chunk.action
                            in {
                                "continue",
                                "closing",
                                "end",
                                "suspend",
                                "safety_lock",
                            }
                            else "continue"
                        )
            self._complete_job(ticket, action)
            self._finish_ticket(
                ticket,
                "completed",
                action=action,
                usage=usage,
                usage_reported=usage_reported,
            )
        except asyncio.CancelledError:
            self._finish_after_error(
                ticket,
                "cancelled",
                "cancelled_no_visible_reply",
                usage=usage,
                usage_reported=usage_reported,
            )
            raise
        except Exception:
            self._finish_after_error(
                ticket,
                "failed",
                "no_visible_reply",
                usage=usage,
                usage_reported=usage_reported,
            )
        finally:
            await self._release_ticket(ticket)

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            if self._shutting_down:
                return
            await self.expire_waiters()

    def _set_running(self, job_id: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = 'running', started_at = ?
                WHERE id = ?
                """,
                (self._clock().isoformat(), job_id),
            )

    def _recover_abandoned_jobs(self) -> None:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, request_id, visitor_id, message_id
                FROM generation_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at, id
                """
            ).fetchall()
        for job_id, request_id, _visitor_id, message_id in rows:
            try:
                self.quota.refund_failed_generation(
                    str(request_id), "restart_cleanup"
                )
            except Exception:
                pass
            self._persist_failed_status(
                str(job_id),
                None if message_id is None else str(message_id),
                "cancelled",
            )

    def _refresh_positions(self) -> None:
        for position, ticket in enumerate(self._waiting, start=1):
            ticket.position = position

    def _remove_expired_waiters(self, now: datetime) -> list[QueueTicket]:
        expired: list[QueueTicket] = []
        retained: deque[QueueTicket] = deque()
        while self._waiting:
            ticket = self._waiting.popleft()
            waited = (now - ticket.queued_at).total_seconds()
            if waited >= self.settings.queue_timeout_seconds:
                expired.append(ticket)
                self._active_visitors.discard(ticket.visitor_id)
            else:
                retained.append(ticket)
        self._waiting = retained
        self._refresh_positions()
        return expired

    def _finish_expired(self, ticket: QueueTicket) -> None:
        try:
            self._persist_failed_generation(
                ticket,
                state="cancelled",
                refund_reason="queue_timeout",
            )
        except Exception:
            pass
        self._finish_ticket(ticket, "queue_timeout")

    def _finish_after_error(
        self,
        ticket: QueueTicket,
        state: str,
        refund_reason: str,
        *,
        usage: dict[str, int] | None = None,
        usage_reported: bool = False,
    ) -> None:
        try:
            self._persist_failed_generation(
                ticket,
                state=state,
                refund_reason=refund_reason,
            )
        except Exception:
            pass
        self._finish_ticket(
            ticket, state, usage=usage, usage_reported=usage_reported
        )

    async def _release_ticket(self, ticket: QueueTicket) -> None:
        async with self._condition:
            if self._active_ticket is ticket:
                self._active_ticket = None
            self._active_visitors.discard(ticket.visitor_id)
            self._condition.notify_all()

    def _append_visible_text(self, job_id: str, text: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE generation_jobs
                SET visible_text = visible_text || ?
                WHERE id = ?
                """,
                (text, job_id),
            )

    def _set_terminal_status(self, job_id: str, status: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, self._clock().isoformat(), job_id),
            )
            conn.execute(
                """
                UPDATE model_calls SET completed_at = ?
                WHERE job_id = ? AND completed_at IS NULL
                """,
                (self._clock().isoformat(), job_id),
            )

    def _persist_failed_generation(
        self,
        ticket: QueueTicket,
        *,
        state: str,
        refund_reason: str,
    ) -> None:
        self.quota.refund_failed_generation(ticket.request_id, refund_reason)
        message_id = None if ticket.request is None else ticket.request.message_id
        self._persist_failed_status(ticket.job_id, message_id, state)

    def _persist_failed_status(
        self, job_id: str, message_id: str | None, state: str
    ) -> None:
        now = self._clock().isoformat()
        with self.database.transaction(immediate=True) as conn:
            if message_id:
                conn.execute(
                    """
                    UPDATE messages SET delivery_status = 'failed'
                    WHERE id = ? AND sender = 'visitor'
                    """,
                    (message_id,),
                )
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?, response_message_id = NULL, finished_at = ?
                WHERE id = ?
                """,
                (state, now, job_id),
            )
            conn.execute(
                """
                UPDATE model_calls SET completed_at = ?
                WHERE job_id = ? AND completed_at IS NULL
                """,
                (now, job_id),
            )

    def _start_model_call(self, job_id: str, visitor_id: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO model_calls (id, visitor_id, job_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid4()), visitor_id, job_id, self._clock().isoformat()),
            )

    def _persist_usage(self, job_id: str, usage: dict[str, int]) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE model_calls
                SET usage_reported = 1, input_tokens = ?, output_tokens = ?
                WHERE job_id = ?
                """,
                (
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    job_id,
                ),
            )

    def _complete_job(self, ticket: QueueTicket, action: str) -> None:
        if action == "safety_lock" and self._safety_template_provider is not None:
            template = self._safety_template_provider().strip()
            if template:
                ticket.visible_text = template
                with self.database.transaction(immediate=True) as conn:
                    conn.execute(
                        "UPDATE generation_jobs SET visible_text = ? WHERE id = ?",
                        (template, ticket.job_id),
                    )
        self._persist_terminal_reply(ticket, state="completed", action=action)

    def _persist_terminal_reply(
        self,
        ticket: QueueTicket,
        *,
        state: str,
        action: str | None = None,
    ) -> None:
        now = self._clock()
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT visible_text, response_message_id
                FROM generation_jobs
                WHERE id = ? AND visitor_id = ?
                """,
                (ticket.job_id, ticket.visitor_id),
            ).fetchone()
            if row is None:
                raise KeyError(ticket.job_id)
            response_message_id = row[1]
            if str(row[0]) and response_message_id is None:
                source = "web" if ticket.request is None else ticket.request.source
                response = _insert_message(
                    conn,
                    ticket.visitor_id,
                    "host",
                    str(row[0]),
                    now,
                    source=source,
                )
                response_message_id = response.id
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?, action = COALESCE(?, action),
                    response_message_id = ?, finished_at = ?
                WHERE id = ? AND visitor_id = ?
                """,
                (
                    state,
                    action,
                    response_message_id,
                    now.isoformat(),
                    ticket.job_id,
                    ticket.visitor_id,
                ),
            )
            conn.execute(
                "UPDATE model_calls SET completed_at = ? WHERE job_id = ?",
                (now.isoformat(), ticket.job_id),
            )
            if action in {"suspend", "safety_lock"}:
                locked_until = (
                    now + SAFETY_LOCK_DURATION if action == "safety_lock" else None
                )
                conn.execute(
                    """
                    UPDATE visitors
                    SET status = ?, safety_locked_until = ?
                    WHERE id = ?
                    """,
                    (
                        "suspended" if action == "suspend" else action,
                        None if locked_until is None else locked_until.isoformat(),
                        ticket.visitor_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE visits SET ended_at = ?
                    WHERE visitor_id = ? AND ended_at IS NULL
                    """,
                    (now.isoformat(), ticket.visitor_id),
                )
                audit_details = {"job_id": ticket.job_id}
                if locked_until is not None:
                    audit_details["locked_until"] = locked_until.isoformat()
                conn.execute(
                    """
                    INSERT INTO audit_events
                        (id, visitor_id, kind, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        ticket.visitor_id,
                        "safety_lock" if action == "safety_lock" else "visitor_suspended",
                        json.dumps(
                            {
                                "actor": "generation",
                                "details": audit_details,
                            },
                            separators=(",", ":"),
                        ),
                        now.isoformat(),
                    ),
                )

    def _finish_ticket(
        self,
        ticket: QueueTicket,
        state: str,
        *,
        action: str = "continue",
        usage: dict[str, int] | None = None,
        usage_reported: bool = False,
    ) -> None:
        already_terminal = ticket._terminal
        ticket.finish(
            state,
            action=action,
            usage=usage,
            usage_reported=usage_reported,
        )
        ticket.drop_sensitive_request()
        if already_terminal:
            return
        self._terminal_tickets.append(ticket.job_id)
        while len(self._terminal_tickets) > self._terminal_ticket_limit:
            expired_job_id = self._terminal_tickets.popleft()
            self._tickets.pop(expired_job_id, None)
