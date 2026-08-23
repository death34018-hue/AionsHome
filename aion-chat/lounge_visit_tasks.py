"""In-process ownership for cancellable lounge visit tasks."""

from __future__ import annotations

import asyncio


class LoungeVisitTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, actor_id: str, task: asyncio.Task) -> None:
        self._tasks[actor_id] = task

    def unregister(self, actor_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(actor_id) is task:
            self._tasks.pop(actor_id, None)

    def cancel(self, actor_id: str) -> bool:
        task = self._tasks.get(actor_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


lounge_visit_tasks = LoungeVisitTaskRegistry()
