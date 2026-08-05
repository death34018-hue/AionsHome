import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class HomecomingSummaryCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "coverage.db"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TABLE homecoming_summary_coverage("
                "owner_id TEXT,message_id TEXT,epoch_id TEXT,"
                "checkpoint_id TEXT,import_session_id TEXT,verified_at REAL,"
                "PRIMARY KEY(owner_id,message_id))"
            )
            await db.executemany(
                "INSERT INTO homecoming_summary_coverage VALUES (?,?,?,?,?,?)",
                [
                    ("main", f"m-{index}", "epoch", "checkpoint", "import", 1)
                    for index in range(30, 120)
                ],
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_middle_coverage_keeps_every_uncovered_message_in_order(self):
        from homecoming.summary_coverage import filter_uncovered

        messages = [
            {"id": f"m-{index}", "created_at": index}
            for index in range(150)
        ]
        async with aiosqlite.connect(self.path) as db:
            uncovered, covered = await filter_uncovered(db, "main", messages)

        self.assertEqual(60, len(uncovered))
        self.assertEqual(
            [f"m-{index}" for index in list(range(30)) + list(range(120, 150))],
            [item["id"] for item in uncovered],
        )
        self.assertEqual(90, len(covered))

    async def test_missing_coverage_table_fails_open(self):
        from homecoming.summary_coverage import filter_uncovered

        missing = Path(self.tmp.name) / "missing.db"
        messages = [{"id": "one", "created_at": 1}]
        async with aiosqlite.connect(missing) as db:
            uncovered, covered = await filter_uncovered(db, "main", messages)

        self.assertEqual(messages, uncovered)
        self.assertEqual(set(), covered)


if __name__ == "__main__":
    unittest.main()
