import contextlib
from datetime import datetime
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from routes import health


SCHEMA = """
CREATE TABLE health_miband_activity (
    source TEXT NOT NULL,
    measured_at REAL NOT NULL,
    device_name TEXT DEFAULT '',
    raw_kind INTEGER NOT NULL DEFAULT 0,
    intensity INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    heart_rate INTEGER NOT NULL DEFAULT 0,
    unknown_value INTEGER NOT NULL DEFAULT 0,
    sleep_value INTEGER NOT NULL DEFAULT 0,
    deep_sleep_value INTEGER NOT NULL DEFAULT 0,
    rem_sleep_value INTEGER NOT NULL DEFAULT 0,
    sleep_stage TEXT DEFAULT '',
    synced_at REAL NOT NULL,
    PRIMARY KEY (source, measured_at)
);
CREATE TABLE health_ring_heart_rates (
    id TEXT PRIMARY KEY, device_name TEXT, heart_rate INTEGER NOT NULL,
    measured_at REAL NOT NULL, source TEXT, raw_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE health_ring_latest (
    id INTEGER PRIMARY KEY, device_name TEXT, heart_rate INTEGER,
    measured_at REAL, raw_json TEXT, synced_at REAL NOT NULL
);
"""


class MiBandActivityApiTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_batch_is_idempotent_and_builds_source_specific_summary(self):
        start = datetime(2026, 7, 17, 0, 0).timestamp()
        body = health.MiBandActivityBatch(
            device_name="Xiaomi Smart Band 7",
            samples=[
                health.MiBandActivitySample(measured_at=start, raw_kind=80, intensity=4, steps=10, heart_rate=70),
                health.MiBandActivitySample(measured_at=start + 60, raw_kind=80, intensity=2, steps=5),
                health.MiBandActivitySample(measured_at=start + 120 * 60, raw_kind=120, sleep_stage="deep"),
                health.MiBandActivitySample(measured_at=start + 121 * 60, raw_kind=120, sleep_stage="light"),
                health.MiBandActivitySample(measured_at=start + 122 * 60, raw_kind=120, sleep_stage="rem"),
            ],
        )
        broadcasts = AsyncMock()
        common = (
            patch.object(health, "get_db", self.get_db),
            patch.object(health, "analyze_heart_rate_entry", AsyncMock(return_value=[])),
            patch.object(health, "get_heart_events", AsyncMock(return_value=[])),
            patch.object(health.manager, "broadcast", broadcasts),
            patch.object(health.time, "time", return_value=start + 12 * 3600),
        )
        with common[0], common[1], common[2], common[3], common[4]:
            first = await health.save_mi_band_activity_batch(body)
            second = await health.save_mi_band_activity_batch(body)

        self.assertEqual(5, first["accepted"])
        self.assertEqual(15, second["miBand"]["todaySteps"])
        self.assertEqual(2, second["miBand"]["activityMinutes"])
        self.assertEqual(70, second["miBand"]["latestHeartRate"])
        self.assertEqual(3, second["miBand"]["sleep"]["totalMin"])
        self.assertEqual(1, second["miBand"]["sleep"]["deepMin"])
        self.assertEqual(1, second["miBand"]["sleep"]["lightMin"])
        self.assertEqual(1, second["miBand"]["sleep"]["remMin"])
        async with aiosqlite.connect(self.db_path) as db:
            activity_count = (await (await db.execute("SELECT COUNT(*) FROM health_miband_activity")).fetchone())[0]
            heart_count = (await (await db.execute("SELECT COUNT(*) FROM health_ring_heart_rates")).fetchone())[0]
        self.assertEqual(5, activity_count)
        self.assertEqual(1, heart_count)

    async def test_summary_never_reuses_legacy_ring_sleep(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO health_ring_latest(id, device_name, heart_rate, measured_at, raw_json, synced_at) VALUES(1,'Smart Ring',66,1,'{}',1)"
            )
            await db.commit()
        with patch.object(health, "get_db", self.get_db):
            async with self.get_db() as db:
                summary = await health.build_mi_band_summary(db, now=datetime(2026, 7, 17, 12, 0).timestamp())
        self.assertIsNone(summary["sleep"])
        self.assertEqual(0, summary["todaySteps"])

    async def test_summary_combines_main_sleep_and_nap_on_latest_sleep_day(self):
        previous_day = datetime(2026, 7, 18, 3, 0).timestamp()
        main_start = datetime(2026, 7, 19, 2, 0).timestamp()
        nap_start = datetime(2026, 7, 19, 9, 0).timestamp()
        samples = [
            (previous_day, "deep"),
            (main_start, "deep"),
            (main_start + 60, "light"),
            (main_start + 120, "light"),
            (nap_start, "light"),
            (nap_start + 60, "deep"),
        ]
        async with aiosqlite.connect(self.db_path) as db:
            for measured_at, stage in samples:
                await db.execute(
                    "INSERT INTO health_miband_activity(source,measured_at,device_name,sleep_stage,synced_at) "
                    "VALUES('mi_band_7',?,?,?,?)",
                    (measured_at, "Xiaomi Smart Band 7", stage, nap_start + 120),
                )
            await db.commit()
            summary = await health.build_mi_band_summary(
                db,
                now=datetime(2026, 7, 19, 12, 0).timestamp(),
            )

        sleep = summary["sleep"]
        self.assertEqual("2026-07-19", sleep["sleepDate"])
        self.assertEqual(5, sleep["totalMin"])
        self.assertEqual(2, sleep["deepMin"])
        self.assertEqual(3, sleep["lightMin"])
        self.assertEqual(main_start, sleep["startAt"])
        self.assertEqual(main_start + 180, sleep["endAt"])
        self.assertEqual("nap", sleep["kind"])
        self.assertEqual(
            [
                {
                    "kind": "nap",
                    "startAt": main_start,
                    "endAt": main_start + 180,
                    "totalMin": 3,
                    "deepMin": 1,
                    "lightMin": 2,
                    "remMin": 0,
                },
                {
                    "kind": "nap",
                    "startAt": nap_start,
                    "endAt": nap_start + 120,
                    "totalMin": 2,
                    "deepMin": 1,
                    "lightMin": 1,
                    "remMin": 0,
                },
            ],
            sleep["sessions"],
        )

    async def test_three_hours_is_classified_as_main_sleep(self):
        start = datetime(2026, 7, 19, 2, 0).timestamp()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO health_miband_activity(source,measured_at,device_name,sleep_stage,synced_at) "
                "VALUES('mi_band_7',?,?,?,?)",
                [
                    (start + minute * 60, "Xiaomi Smart Band 7", "light", start + 180 * 60)
                    for minute in range(180)
                ],
            )
            await db.commit()
            summary = await health.build_mi_band_summary(
                db,
                now=datetime(2026, 7, 19, 12, 0).timestamp(),
            )

        self.assertEqual("main", summary["sleep"]["kind"])
        self.assertEqual("main", summary["sleep"]["sessions"][0]["kind"])

    async def test_summary_reports_recent_activity_minutes_and_steps(self):
        latest = datetime(2026, 7, 19, 12, 0).timestamp()
        samples = [
            (latest - 55 * 60, 20),
            (latest - 20 * 60, 2),
            (latest - 15 * 60, 0),
            (latest, 0),
        ]
        async with aiosqlite.connect(self.db_path) as db:
            for measured_at, steps in samples:
                await db.execute(
                    "INSERT INTO health_miband_activity(source,measured_at,device_name,steps,synced_at) "
                    "VALUES('mi_band_7',?,?,?,?)",
                    (measured_at, "Xiaomi Smart Band 7", steps, latest),
                )
            await db.commit()
            summary = await health.build_mi_band_summary(db, now=latest)

        self.assertEqual(latest, summary["activityDataThrough"])
        self.assertEqual(1, summary["recent30ActivityMinutes"])
        self.assertEqual(2, summary["recent30Steps"])
        self.assertEqual(2, summary["recent60ActivityMinutes"])
        self.assertEqual(22, summary["recent60Steps"])


if __name__ == "__main__":
    unittest.main()
