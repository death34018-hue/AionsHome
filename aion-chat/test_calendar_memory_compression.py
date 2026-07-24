import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def local_ts(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").timestamp()


class CalendarCompressionCandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self):
        return aiosqlite.connect(self.db_path)

    async def asyncSetUp(self):
        async with self._connect() as db:
            await db.execute(
                "CREATE TABLE memories ("
                "id TEXT PRIMARY KEY, content TEXT NOT NULL, type TEXT, created_at REAL, "
                "source_conv TEXT, embedding BLOB, keywords TEXT, importance REAL, "
                "source_start_ts REAL, source_end_ts REAL, source_msg_id TEXT, "
                "compression_stage INTEGER DEFAULT 0, archive_state TEXT DEFAULT 'active', "
                "period_kind TEXT DEFAULT '', period_start_ts REAL, period_end_ts REAL, "
                "compression_batch_id TEXT DEFAULT '')"
            )
            await db.execute(
                "CREATE TABLE chatroom_memories ("
                "id TEXT PRIMARY KEY, room_id TEXT, scope TEXT, content TEXT NOT NULL, "
                "keywords TEXT, importance REAL, embedding BLOB, source_start_ts REAL, "
                "source_end_ts REAL, created_at REAL, unresolved INTEGER DEFAULT 0, "
                "source_msg_id TEXT, memory_kind TEXT DEFAULT 'daily', "
                "compression_stage INTEGER DEFAULT 0, archive_state TEXT DEFAULT 'active', "
                "period_kind TEXT DEFAULT '', period_start_ts REAL, period_end_ts REAL, "
                "compression_batch_id TEXT DEFAULT '')"
            )
            await db.commit()

    async def _insert_main(self, mem_id, when, *, stage=0, archive_state="active", period_kind=""):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO memories ("
                "id, content, type, created_at, source_start_ts, source_end_ts, source_msg_id, "
                "compression_stage, archive_state, period_kind"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    mem_id,
                    f"memory {mem_id}",
                    "daily",
                    when,
                    when,
                    when,
                    json.dumps([f"private:{mem_id}"]),
                    stage,
                    archive_state,
                    period_kind,
                ),
            )
            await db.commit()

    async def test_memory_day_uses_five_am_boundary(self):
        import memory_compression

        before = memory_compression.memory_day_for_ts(local_ts("2026-07-09 04:59"))
        after = memory_compression.memory_day_for_ts(local_ts("2026-07-09 05:00"))

        self.assertEqual(before, "2026-07-08")
        self.assertEqual(after, "2026-07-09")

    async def test_daily_preview_only_counts_active_stage_zero_days_older_than_seven_days(self):
        import memory_compression

        now = local_ts("2026-07-16 05:00")
        await self._insert_main("eligible-a", local_ts("2026-07-08 10:00"))
        await self._insert_main("eligible-b", local_ts("2026-07-08 23:00"))
        await self._insert_main("too-new", local_ts("2026-07-09 10:00"))
        await self._insert_main("already-daily", local_ts("2026-07-01 10:00"), stage=1, period_kind="day")
        await self._insert_main("cold", local_ts("2026-07-01 10:00"), archive_state="cold")

        with patch.object(memory_compression, "get_db", self._connect):
            preview = await memory_compression.compression_preview("main", "daily", now_ts=now)

        self.assertEqual(preview["memory_count"], 2)
        self.assertEqual(preview["period_count"], 1)
        self.assertEqual(preview["periods"][0]["label"], "2026-07-08")
        self.assertEqual(preview["periods"][0]["memory_count"], 2)

    async def test_weekly_preview_waits_until_the_whole_week_is_seven_days_old(self):
        import memory_compression

        now = local_ts("2026-07-16 12:00")  # Thursday
        await self._insert_main("last-week", local_ts("2026-07-08 10:00"), stage=1, period_kind="day")
        await self._insert_main("this-week", local_ts("2026-07-14 10:00"), stage=1, period_kind="day")

        with patch.object(memory_compression, "get_db", self._connect):
            preview = await memory_compression.compression_preview("main", "weekly", now_ts=now)

        self.assertEqual(preview["memory_count"], 0)
        self.assertEqual(preview["period_count"], 0)

    async def test_weekly_preview_requires_no_uncompressed_daily_rows_in_the_week(self):
        import memory_compression

        now = local_ts("2026-07-20 05:00")
        await self._insert_main("compressed-day", local_ts("2026-07-08 10:00"), stage=1, period_kind="day")
        await self._insert_main("raw-day", local_ts("2026-07-09 10:00"), stage=0)

        with patch.object(memory_compression, "get_db", self._connect):
            blocked = await memory_compression.compression_preview("main", "weekly", now_ts=now)

        self.assertEqual(blocked["memory_count"], 0)
        self.assertEqual(blocked["period_count"], 0)

        async with self._connect() as db:
            await db.execute(
                "UPDATE memories SET archive_state='cold' WHERE id='raw-day'"
            )
            await db.commit()

        with patch.object(memory_compression, "get_db", self._connect):
            preview = await memory_compression.compression_preview("main", "weekly", now_ts=now)

        self.assertEqual(preview["memory_count"], 1)
        self.assertEqual(preview["period_count"], 1)
        self.assertEqual(preview["periods"][0]["label"], "2026-07-06 ~ 2026-07-12")

    async def test_monthly_preview_excludes_current_month_and_uses_stage_two_week_capsules(self):
        import memory_compression

        now = local_ts("2026-07-16 12:00")
        await self._insert_main("june-week", local_ts("2026-06-18 10:00"), stage=2, period_kind="week")
        await self._insert_main("july-week", local_ts("2026-07-09 10:00"), stage=2, period_kind="week")

        with patch.object(memory_compression, "get_db", self._connect):
            preview = await memory_compression.compression_preview("main", "monthly", now_ts=now)

        self.assertEqual(preview["memory_count"], 1)
        self.assertEqual(preview["period_count"], 1)
        self.assertEqual(preview["periods"][0]["label"], "2026-06")

    async def test_monthly_preview_waits_until_no_daily_capsules_remain(self):
        import memory_compression

        now = local_ts("2026-07-16 12:00")
        await self._insert_main("june-week", local_ts("2026-06-18 10:00"), stage=2, period_kind="week")
        await self._insert_main("june-day", local_ts("2026-06-20 10:00"), stage=1, period_kind="day")

        with patch.object(memory_compression, "get_db", self._connect):
            preview = await memory_compression.compression_preview("main", "monthly", now_ts=now)

        self.assertEqual(preview["memory_count"], 0)
        self.assertEqual(preview["period_count"], 0)

    async def test_legacy_compressed_daily_memories_become_stage_one_day_capsules_only(self):
        import memory_compression

        old_day = local_ts("2026-06-20 00:00")
        await self._insert_main("legacy-stage-one", old_day, stage=1)
        await self._insert_main("legacy-stage-two", old_day, stage=2)
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO memories ("
                "id, content, type, created_at, source_start_ts, source_end_ts, "
                "compression_stage, archive_state, period_kind"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "long-term",
                    "长期重要",
                    "important",
                    old_day,
                    old_day,
                    old_day,
                    1,
                    "active",
                    "",
                ),
            )
            await db.commit()

        with patch.object(memory_compression, "get_db", self._connect):
            result = await memory_compression.migrate_legacy_daily_capsules()

        self.assertEqual(result["main"], 2)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            migrated = await (
                await db.execute(
                    "SELECT id, compression_stage, period_kind, period_start_ts, period_end_ts "
                    "FROM memories WHERE id LIKE 'legacy-%' ORDER BY id"
                )
            ).fetchall()
            important = await (
                await db.execute(
                    "SELECT compression_stage, period_kind FROM memories WHERE id='long-term'"
                )
            ).fetchone()

        self.assertEqual([row["compression_stage"] for row in migrated], [1, 1])
        self.assertEqual([row["period_kind"] for row in migrated], ["day", "day"])
        self.assertEqual(
            datetime.fromtimestamp(migrated[0]["period_start_ts"]).strftime("%Y-%m-%d %H:%M"),
            "2026-06-20 05:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(migrated[0]["period_end_ts"]).strftime("%Y-%m-%d %H:%M"),
            "2026-06-21 05:00",
        )
        self.assertEqual(important["compression_stage"], 1)
        self.assertEqual(important["period_kind"], "")


class CalendarCompressionRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self):
        return aiosqlite.connect(self.db_path)

    async def asyncSetUp(self):
        async with self._connect() as db:
            await db.execute(
                "CREATE TABLE memories ("
                "id TEXT PRIMARY KEY, content TEXT NOT NULL, type TEXT, created_at REAL, "
                "source_conv TEXT, embedding BLOB, keywords TEXT, importance REAL, "
                "source_start_ts REAL, source_end_ts REAL, unresolved INTEGER DEFAULT 0, "
                "source_msg_id TEXT, compression_stage INTEGER DEFAULT 0, "
                "evidence_summary TEXT DEFAULT '', evidence_detail_level TEXT DEFAULT 'summary', "
                "archive_state TEXT DEFAULT 'active', archived_at REAL, period_kind TEXT DEFAULT '', "
                "period_start_ts REAL, period_end_ts REAL, compression_batch_id TEXT DEFAULT '')"
            )
            await db.commit()

    async def _insert(self, mem_id, when):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO memories ("
                "id, content, type, created_at, source_start_ts, source_end_ts, source_msg_id"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    mem_id,
                    "一条不包含数据库标识的摘要内容",
                    "daily",
                    when,
                    when,
                    when,
                    json.dumps([f"private:{mem_id}"]),
                ),
            )
            await db.commit()

    async def test_zero_candidates_do_not_call_model(self):
        import memory_compression

        model_call = AsyncMock()
        with patch.object(memory_compression, "get_db", self._connect), patch.object(
            memory_compression, "_call_compression_model", model_call
        ), patch.object(memory_compression, "MODELS", {"cheap": {"provider": "test"}}):
            result = await memory_compression.run_calendar_compression(
                "main", "daily", "cheap", now_ts=local_ts("2026-07-16 05:00")
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_candidates")
        model_call.assert_not_awaited()

    async def test_background_job_persists_progress_and_blocks_duplicate_start(self):
        import memory_compression

        await self._insert("job-input", local_ts("2026-07-08 10:00"))
        release = __import__("asyncio").Event()

        async def fake_run(target, level, model_key, *, now_ts=None, progress_callback=None):
            if progress_callback:
                await progress_callback({
                    "completed_calls": 0,
                    "total_calls": 1,
                    "processed_inputs": 0,
                    "created_outputs": 0,
                    "message": "正在调用模型",
                })
            await release.wait()
            return {
                "ok": True,
                "input_count": 1,
                "output_count": 1,
                "model_calls": 1,
                "message": "完成",
            }

        with patch.object(memory_compression, "get_db", self._connect), patch.object(
            memory_compression, "run_calendar_compression", fake_run
        ), patch.object(memory_compression, "MODELS", {"cheap": {"provider": "test"}}):
            first = await memory_compression.create_calendar_compression_job(
                "main", "daily", "cheap"
            )
            await __import__("asyncio").sleep(0.05)
            duplicate = await memory_compression.create_calendar_compression_job(
                "main", "daily", "cheap"
            )
            running = await memory_compression.get_calendar_compression_job(first["job"]["id"])
            release.set()
            await __import__("asyncio").sleep(0.05)
            completed = await memory_compression.get_calendar_compression_job(first["job"]["id"])

        self.assertTrue(first["ok"])
        self.assertEqual(first["job"]["status"], "queued")
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["reason"], "already_running")
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"]["message"], "正在调用模型")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["output_count"], 1)

    async def test_valid_run_archives_inputs_and_records_batch_lineage_without_model_ids(self):
        import memory_compression

        when = local_ts("2026-07-08 10:00")
        await self._insert("old-1", when)
        await self._insert("old-2", when + 60)
        model_call = AsyncMock(
            return_value={
                "periods": [
                    {
                        "period": "2026-07-08",
                        "memories": [
                            {
                                "content": "2026-07-08，完成了值得保留的一件事。",
                                "keywords": ["项目"],
                                "importance": 0.55,
                            }
                        ],
                    }
                ]
            }
        )
        with patch.object(memory_compression, "get_db", self._connect), patch.object(
            memory_compression, "_call_compression_model", model_call
        ), patch.object(memory_compression, "MODELS", {"cheap": {"provider": "test"}}), patch.object(
            memory_compression, "_embedding_for_content", AsyncMock(return_value=None)
        ):
            result = await memory_compression.run_calendar_compression(
                "main", "daily", "cheap", now_ts=local_ts("2026-07-16 05:00")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["input_count"], 2)
        self.assertEqual(result["output_count"], 1)
        sent_prompt = model_call.await_args.args[1]
        self.assertNotIn('"id"', sent_prompt)
        self.assertNotIn("private:", sent_prompt)

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            old_rows = await (
                await db.execute("SELECT id, archive_state, source_msg_id FROM memories WHERE id LIKE 'old-%'")
            ).fetchall()
            output = await (
                await db.execute(
                    "SELECT id, archive_state, compression_stage, period_kind, compression_batch_id "
                    "FROM memories WHERE id NOT LIKE 'old-%'"
                )
            ).fetchone()
            input_links = await (
                await db.execute(
                    "SELECT memory_id FROM memory_compression_batch_inputs ORDER BY memory_id"
                )
            ).fetchall()

        self.assertEqual([row["archive_state"] for row in old_rows], ["cold", "cold"])
        self.assertEqual(json.loads(old_rows[0]["source_msg_id"]), ["private:old-1"])
        self.assertEqual(output["archive_state"], "active")
        self.assertEqual(output["compression_stage"], 1)
        self.assertEqual(output["period_kind"], "day")
        self.assertTrue(output["compression_batch_id"])
        self.assertEqual([row["memory_id"] for row in input_links], ["old-1", "old-2"])

        with patch.object(memory_compression, "get_db", self._connect):
            source_ids = await memory_compression.resolve_source_message_ids("main", output["id"])
        self.assertEqual(source_ids, ["private:old-1", "private:old-2"])

    async def test_missing_period_in_model_output_keeps_every_input_active(self):
        import memory_compression

        await self._insert("day-one", local_ts("2026-07-07 10:00"))
        await self._insert("day-two", local_ts("2026-07-08 10:00"))
        model_call = AsyncMock(
            return_value={
                "periods": [
                    {
                        "period": "2026-07-07",
                        "memories": [{"content": "只返回了一天。", "keywords": [], "importance": 0.4}],
                    }
                ]
            }
        )
        with patch.object(memory_compression, "get_db", self._connect), patch.object(
            memory_compression, "_call_compression_model", model_call
        ), patch.object(memory_compression, "MODELS", {"cheap": {"provider": "test"}}):
            result = await memory_compression.run_calendar_compression(
                "main", "daily", "cheap", now_ts=local_ts("2026-07-16 05:00")
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_model_output")
        async with self._connect() as db:
            states = await (
                await db.execute("SELECT archive_state FROM memories ORDER BY id")
            ).fetchall()
        self.assertEqual([row[0] for row in states], ["active", "active"])


class ColdArchiveRecallSafetyTests(unittest.TestCase):
    def test_main_recall_queries_filter_cold_archive(self):
        source = (ROOT / "memory.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(
            source.count("COALESCE(archive_state,'active')='active'"),
            4,
        )

    def test_chatroom_recall_queries_filter_cold_archive(self):
        source = (ROOT / "chatroom.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(
            source.count("COALESCE(archive_state,'active')='active'"),
            4,
        )


if __name__ == "__main__":
    unittest.main()
