"""Per-identity FIFO lanes shared by browser and MCP visitor turns."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from visitor_lounge.models import GenerationJobRecord, MessageSource
from visitor_lounge.quota import RequestConflict
from visitor_lounge.scheduler import SchedulerShuttingDown


class SubmittedTurn(Protocol):
    job_id: str


class TurnSender(Protocol):
    async def send(
        self,
        visitor_id: str,
        request_id: str,
        text: str,
        source: MessageSource = "web",
    ) -> SubmittedTurn: ...

    async def wait_for_job(
        self, job_id: str, visitor_id: str
    ) -> GenerationJobRecord: ...


class VisitorTurnQueueFull(RuntimeError):
    """Raised before persistence when one visitor already has three waiters."""


@dataclass
class TurnSubmission:
    visitor_id: str
    request_id: str
    text: str
    source: MessageSource
    result: asyncio.Future[SubmittedTurn]


class VisitorTurnCoordinator:
    """Serialize one identity before prompt construction, not only at generation."""

    def __init__(
        self, sender: TurnSender, max_waiting_per_visitor: int = 3
    ) -> None:
        if max_waiting_per_visitor < 0:
            raise ValueError("max_waiting_per_visitor must not be negative")
        self._sender = sender
        self._max_waiting_per_visitor = max_waiting_per_visitor
        self._lanes: dict[str, deque[TurnSubmission]] = {}
        self._request_owners: dict[str, str] = {}
        self._request_submissions: dict[str, TurnSubmission] = {}
        self._lane_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    async def submit(
        self,
        visitor_id: str,
        request_id: str,
        text: str,
        *,
        source: MessageSource,
    ) -> SubmittedTurn:
        async with self._lock:
            if self._shutting_down:
                raise SchedulerShuttingDown()
            owner = self._request_owners.get(request_id)
            if owner is not None:
                if owner != visitor_id:
                    raise RequestConflict(request_id)
                submission = self._request_submissions[request_id]
            else:
                lane = self._lanes.setdefault(visitor_id, deque())
                if len(lane) >= self._max_waiting_per_visitor + 1:
                    raise VisitorTurnQueueFull(visitor_id)
                submission = TurnSubmission(
                    visitor_id=visitor_id,
                    request_id=request_id,
                    text=text,
                    source=source,
                    result=asyncio.get_running_loop().create_future(),
                )
                lane.append(submission)
                self._request_owners[request_id] = visitor_id
                self._request_submissions[request_id] = submission
                if visitor_id not in self._lane_tasks:
                    self._lane_tasks[visitor_id] = asyncio.create_task(
                        self._run_lane(visitor_id),
                        name=f"visitor-turn-{visitor_id}",
                    )
        return await asyncio.shield(submission.result)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._shutting_down:
                tasks = list(self._lane_tasks.values())
            else:
                self._shutting_down = True
                error = SchedulerShuttingDown()
                for lane in self._lanes.values():
                    for submission in lane:
                        if not submission.result.done():
                            submission.result.set_exception(error)
                tasks = list(self._lane_tasks.values())
                for task in tasks:
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._lanes.clear()
            self._request_owners.clear()
            self._request_submissions.clear()
            self._lane_tasks.clear()

    async def _run_lane(self, visitor_id: str) -> None:
        while True:
            async with self._lock:
                lane = self._lanes.get(visitor_id)
                if not lane:
                    self._lanes.pop(visitor_id, None)
                    self._lane_tasks.pop(visitor_id, None)
                    return
                submission = lane[0]
            try:
                ticket = await self._sender.send(
                    submission.visitor_id,
                    submission.request_id,
                    submission.text,
                    source=submission.source,
                )
                if not submission.result.done():
                    submission.result.set_result(ticket)
                await self._sender.wait_for_job(ticket.job_id, submission.visitor_id)
            except asyncio.CancelledError:
                if not submission.result.done():
                    submission.result.set_exception(SchedulerShuttingDown())
                raise
            except BaseException as error:
                if not submission.result.done():
                    submission.result.set_exception(error)
            finally:
                lane_finished = False
                async with self._lock:
                    lane = self._lanes.get(visitor_id)
                    if lane and lane[0] is submission:
                        lane.popleft()
                    if self._request_submissions.get(submission.request_id) is submission:
                        self._request_submissions.pop(submission.request_id, None)
                        self._request_owners.pop(submission.request_id, None)
                    if lane is not None and not lane:
                        self._lanes.pop(visitor_id, None)
                        self._lane_tasks.pop(visitor_id, None)
                        lane_finished = True
            if lane_finished:
                return
