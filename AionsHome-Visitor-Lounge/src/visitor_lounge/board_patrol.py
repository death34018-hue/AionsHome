"""Independent, persistent message-board timers; empty checks never wake a model."""

import asyncio
import random
import time

from visitor_lounge.database import Database


ACTORS = ("aion", "connor")


class PatrolStore:
    def __init__(self, database: Database):
        self.database = database

    def config(self, actor: str) -> dict:
        with self.database.transaction(immediate=True) as conn:
            return self._read_config(conn, actor)

    def _read_config(self, conn, actor: str) -> dict:
        if actor not in ACTORS:
            raise ValueError("家庭成员不存在")
        conn.execute("INSERT OR IGNORE INTO board_patrol_configs(actor_id) VALUES (?)", (actor,))
        row = conn.execute(
            "SELECT actor_id,enabled,min_interval_minutes,max_interval_minutes,next_check_at,"
            "last_checked_at,last_status,last_error FROM board_patrol_configs WHERE actor_id=?",
            (actor,),
        ).fetchone()
        result = dict(zip(("actor_id", "enabled", "min_interval_minutes", "max_interval_minutes",
                           "next_check_at", "last_checked_at", "last_status", "last_error"), row))
        result["enabled"] = bool(result["enabled"])
        return result

    def update(self, actor: str, *, enabled: bool | None = None,
               min_interval_minutes: int | None = None, max_interval_minutes: int | None = None,
               now: float | None = None) -> dict:
        with self.database.transaction(immediate=True) as conn:
            current = self._read_config(conn, actor)
            active = current["enabled"] if enabled is None else enabled
            low = current["min_interval_minutes"] if min_interval_minutes is None else min_interval_minutes
            high = current["max_interval_minutes"] if max_interval_minutes is None else max_interval_minutes
            if not 5 <= low <= high <= 1440:
                raise ValueError("巡看间隔需在 5 分钟到 24 小时之间，最短不能大于最长")
            changed = low != current["min_interval_minutes"] or high != current["max_interval_minutes"]
            next_check = current["next_check_at"]
            if not active:
                next_check = None
            elif not current["enabled"] or changed:
                anchor = time.time() if now is None else now
                next_check = anchor + random.randint(low, high) * 60
            conn.execute(
                "UPDATE board_patrol_configs SET enabled=?,min_interval_minutes=?,"
                "max_interval_minutes=?,next_check_at=? WHERE actor_id=?",
                (int(active), low, high, next_check, actor),
            )
            return self._read_config(conn, actor)

    def claim_due(self, actor: str, now: float) -> bool:
        with self.database.transaction(immediate=True) as conn:
            cursor = conn.execute(
                "UPDATE board_patrol_configs SET next_check_at=NULL WHERE actor_id=? "
                "AND enabled=1 AND next_check_at IS NOT NULL AND next_check_at<=?",
                (actor, now),
            )
            return bool(cursor.rowcount)

    def finish(self, actor: str, status: str, error: str = "", *, now: float | None = None) -> None:
        anchor = time.time() if now is None else now
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT enabled,min_interval_minutes,max_interval_minutes,next_check_at "
                "FROM board_patrol_configs WHERE actor_id=?", (actor,),
            ).fetchone()
            # A settings change during the round owns its new deadline.
            next_check = row[3]
            if not row[0]:
                next_check = None
            elif next_check is None:
                next_check = anchor + random.randint(row[1], row[2]) * 60
            conn.execute(
                "UPDATE board_patrol_configs SET next_check_at=?,last_checked_at=?,"
                "last_status=?,last_error=? WHERE actor_id=?",
                (next_check, anchor, status, error[:300], actor),
            )

    def restore(self) -> None:
        """Recover a check interrupted by shutdown; overdue timers run once, never catch up."""
        for actor in ACTORS:
            cfg = self.config(actor)
            if cfg["enabled"] and cfg["next_check_at"] is None:
                self.finish(actor, "interrupted")


class BoardPatrolManager:
    def __init__(self, store: PatrolStore | None = None):
        self._store = store
        self._task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}

    def _current_store(self) -> PatrolStore:
        from visitor_lounge.home_board import BOARD
        return self._store or PatrolStore(BOARD.database)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [task for task in [self._task, *self._running.values()] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()
        self._task = None

    async def _loop(self) -> None:
        from visitor_lounge.home_board import _enabled, _require_enabled

        restored = False
        while True:
            try:
                if _enabled():
                    _require_enabled()
                    store = self._current_store()
                    if not restored:
                        store.restore()
                        restored = True
                    now = time.time()
                    for actor in ACTORS:
                        running = self._running.get(actor)
                        if running is not None and not running.done():
                            continue
                        cfg = store.config(actor)
                        if cfg["enabled"] and cfg["next_check_at"] is not None and cfg["next_check_at"] <= now:
                            # Each actor runs separately; a slow response cannot hold up the other timer.
                            self._running[actor] = asyncio.create_task(self.run_due(actor))
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[board_patrol] check error: {type(error).__name__}")
                await asyncio.sleep(30)

    async def run_due(self, actor: str, *, now: float | None = None) -> dict:
        from visitor_lounge.home_board import _enabled, _require_enabled, inspect_actor

        if not _enabled():
            return {"status": "disabled"}
        _require_enabled()
        store = self._current_store()
        timestamp = time.time() if now is None else now
        store.config(actor)
        if not store.claim_due(actor, timestamp):
            return {"status": "not_due"}
        status, error = "interrupted", ""
        try:
            result = await inspect_actor(actor, scheduled=True)
            status = str(result["status"])
            return result
        except Exception as exc:
            status, error = "failed", str(exc)
            return {"status": status, "error": error}
        finally:
            store.finish(actor, status, error, now=now)


board_patrol_mgr = BoardPatrolManager()
