import contextlib
from datetime import datetime
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite

import context_builder


SCHEMA = """
CREATE TABLE health_ring_latest (
    id INTEGER PRIMARY KEY, device_name TEXT, heart_rate INTEGER,
    systolic_bp INTEGER, diastolic_bp INTEGER, spo2 INTEGER, hrv REAL,
    measured_at REAL, sleep_total_min INTEGER, sleep_deep_min INTEGER,
    sleep_light_min INTEGER, sleep_rem_min INTEGER, sleep_wake_min INTEGER,
    sleep_wake_count INTEGER, raw_json TEXT, synced_at REAL NOT NULL
);
CREATE TABLE health_miband_activity (
    source TEXT NOT NULL, measured_at REAL NOT NULL, device_name TEXT,
    raw_kind INTEGER DEFAULT 0, intensity INTEGER DEFAULT 0, steps INTEGER DEFAULT 0,
    heart_rate INTEGER DEFAULT 0, unknown_value INTEGER DEFAULT 0,
    sleep_value INTEGER DEFAULT 0, deep_sleep_value INTEGER DEFAULT 0,
    rem_sleep_value INTEGER DEFAULT 0, sleep_stage TEXT DEFAULT '',
    synced_at REAL NOT NULL, PRIMARY KEY (source, measured_at)
);
CREATE TABLE health_ring_heart_rates (
    id TEXT PRIMARY KEY, device_name TEXT, heart_rate INTEGER NOT NULL,
    measured_at REAL NOT NULL, source TEXT, raw_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE health_heart_config (
    id INTEGER PRIMARY KEY, sleep_low_max INTEGER DEFAULT 65,
    normal_min INTEGER DEFAULT 70, normal_max INTEGER DEFAULT 95,
    elevated_min INTEGER DEFAULT 96, exercise_min INTEGER DEFAULT 100,
    attention_low INTEGER DEFAULT 45, attention_high INTEGER DEFAULT 135,
    large_delta INTEGER DEFAULT 25, night_start_hour INTEGER DEFAULT 0,
    night_end_hour INTEGER DEFAULT 6, stale_minutes INTEGER DEFAULT 30,
    updated_at REAL DEFAULT 0
);
CREATE TABLE health_weight_entries (
    date TEXT PRIMARY KEY, weight_kg REAL, note TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE health_period_entries (
    id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT, flow TEXT,
    symptoms TEXT, note TEXT, created_at REAL, updated_at REAL
);
"""


class MiBandAiHealthContextTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "health.db"
        self.now = datetime(2026, 7, 17, 12, 0).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.execute(
                "INSERT INTO health_ring_latest(id,device_name,heart_rate,systolic_bp,diastolic_bp,spo2,hrv,"
                "measured_at,sleep_total_min,sleep_deep_min,sleep_light_min,sleep_rem_min,sleep_wake_min,"
                "sleep_wake_count,raw_json,synced_at) VALUES(1,'Old Smart Ring',61,106,70,98,30,?,?,?,?,?,?,?,?,?)",
                (self.now - 60, 355, 79, 184, 92, 0, 0, "{}", self.now),
            )
            samples = [
                (self.now - 50 * 60, "", 2, 0),
                (self.now - 180, "", 8, 74),
                (self.now - 120, "", 12, 75),
                (self.now - 3 * 3600, "deep", 0, 0),
                (self.now - 3 * 3600 + 60, "light", 0, 0),
                (self.now - 3 * 3600 + 120, "rem", 0, 0),
                (self.now - 2 * 3600, "light", 0, 0),
                (self.now - 2 * 3600 + 60, "deep", 0, 0),
            ]
            for measured, stage, steps, heart in samples:
                await db.execute(
                    "INSERT INTO health_miband_activity(source,measured_at,device_name,steps,heart_rate,sleep_stage,synced_at) "
                    "VALUES('mi_band_7',?,?,?,?,?,?)",
                    (measured, "Xiaomi Smart Band 7", steps, heart, stage, self.now),
                )
            await db.execute(
                "INSERT INTO health_weight_entries VALUES('2026-07-17',64.4,'',?,?)",
                (self.now, self.now),
            )
            await db.execute(
                "INSERT INTO health_period_entries VALUES('p1','2026-06-24','','','','',?,?)",
                (self.now, self.now),
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

    async def test_ai_summary_uses_only_mi_band_wearable_data(self):
        with patch.object(context_builder, "get_db", self.get_db), \
             patch.object(context_builder, "is_capability_enabled", return_value=True), \
             patch.object(context_builder.time, "time", return_value=self.now):
            summary = await context_builder.build_health_summary()

        self.assertIn("心率:75", summary)
        self.assertIn("今日步数:22", summary)
        self.assertIn("活动:3分钟", summary)
        self.assertIn("最近30分钟：活动2分钟，20步", summary)
        self.assertIn("最近60分钟：活动3分钟，22步", summary)
        self.assertIn(
            "睡眠:总计5m 小睡09:00-09:03(3m) 小睡10:00-10:02(2m) 深睡2m 浅睡2m REM1m",
            summary,
        )
        self.assertIn("体重:64.4kg", summary)
        self.assertIn("上次例假:2026-06-24", summary)
        self.assertNotIn("血压", summary)
        self.assertNotIn("血氧", summary)
        self.assertNotIn("HRV", summary)
        self.assertNotIn("心率:61", summary)
        self.assertNotIn("深睡79m", summary)


if __name__ == "__main__":
    unittest.main()
