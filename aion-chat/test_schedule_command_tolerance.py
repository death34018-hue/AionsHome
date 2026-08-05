import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

import schedule


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


class ScheduleCommandToleranceTests(unittest.IsolatedAsyncioTestCase):
    def test_schedule_protocols_accept_spacing_fullwidth_symbols_and_case(self):
        cases = [
            (schedule.ALARM_CMD, "［ alarm ： 2026-08-05 18:00 ｜ 喝水 ］", ("2026-08-05 18:00", "喝水")),
            (schedule.REMINDER_CMD, "[ REMINDER：2026-08-06 09:00|开会 ]", ("2026-08-06 09:00", "开会")),
            (schedule.MONITOR_CMD, "［ MONITOR: 2026-08-06 10:00｜检查状态］", ("2026-08-06 10:00", "检查状态")),
            (schedule.SCHEDULE_DEL_CMD, "[ SCHEDULE_DEL ： sch_123 ]", ("sch_123",)),
            (schedule.SCHEDULE_LIST_CMD, "［ schedule_list ］", ()),
        ]

        for pattern, text, expected_groups in cases:
            with self.subTest(text=text):
                match = pattern.search(text)
                self.assertIsNotNone(match)
                self.assertEqual(tuple(group.strip() for group in match.groups()), expected_groups)
                self.assertEqual(pattern.sub("", f"正文{text}结尾"), "正文结尾")

    async def test_fullwidth_delete_command_cancels_schedule_and_is_hidden(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        db_path = Path(tempdir.name) / "schedule.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(CREATE_SCHEDULES)
            await db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                ("sch_123", "monitor", "2026-08-06 10:00", "检查", 1.0, "active", "connor", "room", None),
            )
            await db.commit()

        def connection():
            return aiosqlite.connect(db_path)

        with patch.object(schedule, "get_db", connection), patch.object(
            schedule.manager, "broadcast", AsyncMock()
        ):
            visible = await schedule.process_schedule_commands(
                "已经取消。［ SCHEDULE_DEL ： sch_123 ］",
                None,
                origin="connor",
                origin_room_id="room",
            )

        async with aiosqlite.connect(db_path) as db:
            row = await (
                await db.execute("SELECT status FROM schedules WHERE id='sch_123'")
            ).fetchone()

        self.assertEqual(visible, "已经取消。")
        self.assertEqual(row, ("cancelled",))


if __name__ == "__main__":
    unittest.main()
