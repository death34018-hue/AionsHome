import contextlib
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from routes import health


SCHEMA = """
CREATE TABLE health_ring_heart_rates (
    id TEXT PRIMARY KEY, device_name TEXT, heart_rate INTEGER NOT NULL,
    measured_at REAL NOT NULL, source TEXT, raw_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE health_ring_latest (
    id INTEGER PRIMARY KEY, device_name TEXT, heart_rate INTEGER,
    measured_at REAL, raw_json TEXT, synced_at REAL NOT NULL
);
"""


class HealthMiBandIngestionTest(unittest.IsolatedAsyncioTestCase):
    def test_health_prompts_are_source_neutral(self):
        root = pathlib.Path(__file__).resolve().parent
        for relative in ("health_context.py", "camera.py"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("戒指数据，仅作辅助", text)
            self.assertIn("穿戴设备数据，仅作辅助", text)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "health.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
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

    async def test_miband_endpoint_deduplicates_and_analyzes_only_new_sample(self):
        analyzed = AsyncMock(return_value=[])
        broadcasts = AsyncMock()
        body = health.HeartRateSample(
            device_name="Xiaomi Smart Band 7",
            heart_rate=72,
            measured_at=1_752_710_400.0,
            source="mi_band_7",
            raw={"steps": 3, "intensity": 4},
        )
        with patch.object(health, "get_db", self.get_db), \
             patch.object(health, "analyze_heart_rate_entry", analyzed), \
             patch.object(health, "get_heart_events", AsyncMock(return_value=[])), \
             patch.object(health.manager, "broadcast", broadcasts):
            first = await health.save_heart_rate(body)
            second = await health.save_heart_rate(body)

        self.assertTrue(first["entry"]["is_new"])
        self.assertFalse(second["entry"]["is_new"])
        self.assertEqual(1, analyzed.await_count)
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*), source, raw_json FROM health_ring_heart_rates"
            )).fetchone()
        self.assertEqual(1, row[0])
        self.assertEqual("mi_band_7", row[1])
        self.assertIn('"steps": 3', row[2])

    async def test_legacy_ring_endpoint_uses_shared_ingestion(self):
        body = health.RingHeartRate(
            device_name="Smart Ring",
            heart_rate=68,
            measured_at=1_752_710_500.0,
            source="ring_realtime",
        )
        with patch.object(health, "get_db", self.get_db), \
             patch.object(health, "analyze_heart_rate_entry", AsyncMock(return_value=[])), \
             patch.object(health, "get_heart_events", AsyncMock(return_value=[])), \
             patch.object(health.manager, "broadcast", AsyncMock()):
            result = await health.save_ring_heart_rate(body)

        self.assertEqual("ring_realtime", result["entry"]["source"])

    def test_neutral_route_is_registered(self):
        source = pathlib.Path(health.__file__).read_text(encoding="utf-8")
        self.assertIn('@router.post("/heart-rate")', source)


if __name__ == "__main__":
    unittest.main()
