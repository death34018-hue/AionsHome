import contextlib
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from routes import health

ROOT = pathlib.Path(__file__).resolve().parent
HTML = (ROOT / "static" / "health.html").read_text(encoding="utf-8")

SCHEMA = """
CREATE TABLE health_weight_entries (
    date TEXT PRIMARY KEY, weight_kg REAL NOT NULL, note TEXT DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE health_period_entries (
    id TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT DEFAULT '',
    flow TEXT DEFAULT '', symptoms TEXT DEFAULT '', note TEXT DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


class HealthBodyPageTest(unittest.TestCase):
    def test_weight_chart_is_loaded_and_wired(self):
        self.assertIn('<script src="/static/health-weight-chart.js', HTML)
        self.assertIn('id="weightChart"', HTML)
        for value in ("7", "30", "90", "all"):
            self.assertIn(f'data-range="{value}"', HTML)
        self.assertIn("api('GET', '/api/health/weights?limit=1000')", HTML)
        self.assertIn("window.HealthWeightChart.create($('weightChart')", HTML)

    def test_record_sheets_support_edit_and_delete(self):
        self.assertIn('class="health-modal sheet" id="weightModal"', HTML)
        self.assertIn('class="health-modal sheet" id="periodModal"', HTML)
        self.assertIn('id="weightDeleteBtn"', HTML)
        self.assertIn('id="periodDeleteBtn"', HTML)
        self.assertIn('data-weight-step="-0.1"', HTML)
        self.assertIn('data-weight-step="0.1"', HTML)
        self.assertIn("api('DELETE', `/api/health/weights/${encodeURIComponent(date)}`)", HTML)
        self.assertIn("$('calendarGrid').addEventListener('click', onCalendarClick)", HTML)

    def test_band_heart_history_is_collapsed_by_default(self):
        self.assertIn('<details class="heart-history heart-history-fold" id="miBandHistoryFold">', HTML)
        start = HTML.index('id="miBandHistoryFold"')
        end = HTML.index('</details>', start)
        self.assertIn('id="miBandRecentList"', HTML[start:end])

    def test_page_applies_delete_broadcasts(self):
        self.assertIn("msg.type === 'health_weight_deleted'", HTML)
        self.assertIn("msg.type === 'health_period_deleted'", HTML)


class HealthDeleteBroadcastTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "health.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.execute("INSERT INTO health_weight_entries VALUES('2026-11-10', 66.4, '', 1, 1)")
            await db.execute("INSERT INTO health_period_entries VALUES('hp_1', '2026-11-09', '', '', '', '', 1, 1)")
            await db.commit()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def get_db(self):
        path = self.db_path

        @contextlib.asynccontextmanager
        async def manager():
            db = await aiosqlite.connect(path)
            try:
                yield db
            finally:
                await db.close()

        return manager()

    async def test_delete_weight_and_period_broadcast(self):
        broadcasts = AsyncMock()
        with patch.object(health, "get_db", self.get_db), patch.object(health.manager, "broadcast", broadcasts):
            self.assertEqual(await health.delete_weight("2026-11-10"), {"ok": True})
            self.assertEqual(await health.delete_period("hp_1"), {"ok": True})
        broadcasts.assert_any_await({"type": "health_weight_deleted", "data": {"date": "2026-11-10"}})
        broadcasts.assert_any_await({"type": "health_period_deleted", "data": {"id": "hp_1"}})
        async with aiosqlite.connect(self.db_path) as db:
            for table in ("health_weight_entries", "health_period_entries"):
                cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
                self.assertEqual((await cur.fetchone())[0], 0)

    async def test_invalid_weight_date_does_not_broadcast(self):
        broadcasts = AsyncMock()
        with patch.object(health, "get_db", self.get_db), patch.object(health.manager, "broadcast", broadcasts):
            result = await health.delete_weight("not-a-date")
        self.assertIn("error", result)
        broadcasts.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
