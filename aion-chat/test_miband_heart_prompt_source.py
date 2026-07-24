import contextlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

import health_context


class MiBandHeartPromptSourceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "heart.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE health_ring_heart_rates(id TEXT PRIMARY KEY,device_name TEXT,heart_rate INTEGER,"
                "measured_at REAL,source TEXT,raw_json TEXT,created_at REAL)"
            )
            await db.execute(
                "INSERT INTO health_ring_heart_rates VALUES('ring','Old Ring',61,2000,'ring_realtime','{}',2000)"
            )
            await db.execute(
                "INSERT INTO health_ring_heart_rates VALUES('band','Mi Band',75,1900,'mi_band_7','{}',1900)"
            )
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

    async def test_passive_prompt_excludes_ring_samples_and_ring_events(self):
        config = {
            "sleep_low_max": 65, "normal_min": 70, "normal_max": 95,
            "elevated_min": 96, "exercise_min": 100, "attention_low": 45,
            "attention_high": 135, "large_delta": 25, "night_start_hour": 0,
            "night_end_hour": 6, "stale_minutes": 30,
        }
        events = [
            {"event_type": "large_delta", "summary": "旧戒指事件", "created_at": 1990,
             "details_json": json.dumps({"source": "ring_realtime"})},
            {"event_type": "exercise_candidate", "summary": "手环事件", "created_at": 1990,
             "details_json": json.dumps({"source": "mi_band_7"})},
        ]
        with patch.object(health_context, "get_db", self.get_db), \
             patch.object(health_context, "get_heart_config", AsyncMock(return_value=config)), \
             patch.object(health_context, "get_heart_events", AsyncMock(return_value=events)), \
             patch.object(health_context.time, "time", return_value=2000):
            summary = await health_context.build_heart_rate_summary_for_prompt()

        self.assertIn("最近心率：75", summary)
        self.assertIn("手环事件", summary)
        self.assertNotIn("61", summary)
        self.assertNotIn("旧戒指事件", summary)


if __name__ == "__main__":
    unittest.main()
