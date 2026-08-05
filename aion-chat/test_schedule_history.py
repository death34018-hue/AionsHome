import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schedule_history import fetch_schedule_history, finish_schedule, migrate_schedule_history


CREATE_SCHEDULES = """
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT DEFAULT 'aion',
    origin_room_id TEXT DEFAULT '',
    ended_at REAL
)
"""


class ScheduleHistoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "history.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_SCHEDULES)
            await db.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_finish_records_real_end_time_once(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                ("one", "alarm", "2026-07-26 08:00", "wake", 10.0, "active", "aion", "", None),
            )
            self.assertTrue(await finish_schedule(db, "one", "triggered", ended_at=100.0))
            self.assertFalse(await finish_schedule(db, "one", "cancelled", ended_at=200.0))
            await db.commit()
            row = await (
                await db.execute("SELECT status, ended_at FROM schedules WHERE id='one'")
            ).fetchone()

        self.assertEqual(row, ("triggered", 100.0))

    async def test_finishing_the_31st_history_deletes_only_the_oldest_history(self):
        async with aiosqlite.connect(self.db_path) as db:
            for index in range(1, 31):
                await db.execute(
                    "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"old-{index}",
                        ("alarm", "reminder", "monitor")[index % 3],
                        "2026-07-26 08:00",
                        f"old {index}",
                        float(index),
                        "triggered" if index % 2 else "cancelled",
                        "aion",
                        "",
                        float(index),
                    ),
                )
            await db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                ("still-active", "reminder", "2026-07-27 08:00", "future", 50.0, "active", "user", "", None),
            )
            await db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                ("newest", "monitor", "2026-07-26 09:00", "check", 60.0, "active", "connor", "", None),
            )

            self.assertTrue(await finish_schedule(db, "newest", "cancelled", ended_at=31.0))
            await db.commit()
            history = await fetch_schedule_history(db)
            active = await (
                await db.execute("SELECT id FROM schedules WHERE status='active'")
            ).fetchall()

        self.assertEqual(len(history), 30)
        self.assertEqual(history[0]["id"], "newest")
        self.assertNotIn("old-1", {item["id"] for item in history})
        self.assertEqual(active, [("still-active",)])

    async def test_finish_rejects_non_history_status(self):
        async with aiosqlite.connect(self.db_path) as db:
            with self.assertRaises(ValueError):
                await finish_schedule(db, "missing", "active", ended_at=1.0)


class ScheduleHistoryMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "legacy.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                CREATE_SCHEDULES.replace(",\n    ended_at REAL", "")
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_migration_backfills_triggered_and_cancelled_end_times(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("triggered", "alarm", "2026-07-26 12:34", "done", 10.0, "triggered", "aion", ""),
                    ("cancelled", "monitor", "invalid", "stopped", 25.0, "cancelled", "connor", ""),
                    ("active", "reminder", "2026-08-01 09:00", "future", 30.0, "active", "user", ""),
                ],
            )

            await migrate_schedule_history(db)
            await db.commit()
            rows = await (
                await db.execute("SELECT id, ended_at FROM schedules ORDER BY id")
            ).fetchall()

        values = dict(rows)
        self.assertEqual(values["triggered"], datetime.fromisoformat("2026-07-26 12:34").timestamp())
        self.assertEqual(values["cancelled"], 25.0)
        self.assertIsNone(values["active"])

    async def test_migration_physically_trims_legacy_history_to_30_rows(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        f"legacy-{index}",
                        "alarm",
                        "invalid",
                        f"history {index}",
                        float(index),
                        "triggered" if index % 2 else "cancelled",
                        "aion",
                        "",
                    )
                    for index in range(1, 33)
                ],
            )

            await migrate_schedule_history(db)
            await db.commit()
            rows = await (
                await db.execute("SELECT id FROM schedules ORDER BY ended_at DESC")
            ).fetchall()

        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0], ("legacy-32",))
        self.assertNotIn(("legacy-1",), rows)
        self.assertNotIn(("legacy-2",), rows)


class ScheduleHistoryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "routes.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_SCHEDULES)
            await db.executemany(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("active", "alarm", "2026-07-27 08:00", "future", 1.0, "active", "aion", "", None),
                    ("older", "reminder", "2026-07-26 08:00", "plan", 2.0, "triggered", "aion", "", 10.0),
                    ("newer", "monitor", "2026-07-26 09:00", "check", 3.0, "cancelled", "connor", "", 20.0),
                ],
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def connection(self):
        return aiosqlite.connect(self.db_path)

    async def test_history_query_returns_only_ended_rows_newest_first_with_origin_names(self):
        import routes.schedule as route

        with patch.object(route, "get_db", self.connection), patch.object(
            route,
            "get_schedule_origin_name",
            side_effect=lambda origin: {
                "aion": "Configured Main",
                "connor": "Configured Companion",
                "user": "Configured User",
            }[origin],
        ):
            rows = await route.list_schedules("history")

        self.assertEqual([row["id"] for row in rows], ["newer", "older"])
        self.assertEqual(
            [row["origin_name"] for row in rows],
            ["Configured Companion", "Configured Main"],
        )
        self.assertEqual([row["status"] for row in rows], ["cancelled", "triggered"])

    async def test_route_deletion_moves_active_row_to_cancelled_history(self):
        import routes.schedule as route

        broadcast = AsyncMock()
        with patch.object(route, "get_db", self.connection), patch.object(
            route.manager, "broadcast", broadcast
        ):
            result = await route.delete_schedule("active")

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT status, ended_at FROM schedules WHERE id='active'"
                )
            ).fetchone()

        self.assertEqual(result, {"ok": True})
        self.assertEqual(row[0], "cancelled")
        self.assertIsInstance(row[1], float)
        broadcast.assert_awaited_once_with({"type": "schedule_changed"})

    async def test_text_command_deletion_uses_the_same_history_transition(self):
        import schedule

        broadcast = AsyncMock()
        with patch.object(schedule, "get_db", self.connection), patch.object(
            schedule.manager, "broadcast", broadcast
        ):
            await schedule._del_schedule("active")

        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT status, ended_at FROM schedules WHERE id='active'"
                )
            ).fetchone()

        self.assertEqual(row[0], "cancelled")
        self.assertIsInstance(row[1], float)
        broadcast.assert_awaited_once_with({"type": "schedule_changed"})

    async def test_duplicate_route_deletion_is_a_quiet_noop(self):
        import routes.schedule as route

        broadcast = AsyncMock()
        with patch.object(route, "get_db", self.connection), patch.object(
            route.manager, "broadcast", broadcast
        ):
            result = await route.delete_schedule("older")

        self.assertEqual(result, {"ok": False})
        broadcast.assert_not_awaited()


class ScheduleHistoryRuntimeRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "race.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_SCHEDULES)
            await db.executemany(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("alarm-race", "alarm", "2026-07-26 08:00", "wake", 1.0, "cancelled", "aion", "", 2.0),
                    ("monitor-race", "monitor", "2026-07-26 08:00", "check", 1.0, "cancelled", "connor", "", 2.0),
                ],
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_cancelled_alarm_snapshot_does_not_broadcast_or_continue(self):
        import schedule

        manager = schedule.ScheduleManager()
        manager._resolve_target = Mock(side_effect=AssertionError("cancelled alarm continued"))
        broadcast = AsyncMock()
        item = {
            "id": "alarm-race",
            "type": "alarm",
            "trigger_at": "2026-07-26 08:00",
            "content": "wake",
            "origin": "aion",
        }

        with patch.object(schedule, "DB_PATH", self.db_path), patch.object(
            schedule.manager, "broadcast", broadcast
        ):
            await manager._fire_alarm(item)

        manager._resolve_target.assert_not_called()
        broadcast.assert_not_awaited()

    async def test_cancelled_monitor_snapshot_does_not_broadcast_or_continue(self):
        import schedule

        manager = schedule.ScheduleManager()
        manager._resolve_target = Mock(side_effect=AssertionError("cancelled monitor continued"))
        broadcast = AsyncMock()
        item = {
            "id": "monitor-race",
            "type": "monitor",
            "trigger_at": "2026-07-26 08:00",
            "content": "check",
            "origin": "connor",
        }

        with patch.object(schedule, "DB_PATH", self.db_path), patch.object(
            schedule.manager, "broadcast", broadcast
        ):
            await manager._fire_monitor(item)

        manager._resolve_target.assert_not_called()
        broadcast.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
