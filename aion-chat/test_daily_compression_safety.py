import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DailyCompressionApplySafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"

    def tearDown(self):
        self.tmp.cleanup()

    async def _create_review(self, *, status="draft", apply_result=None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE daily_memory_compress_reviews ("
                "id TEXT PRIMARY KEY, target TEXT NOT NULL DEFAULT 'main', "
                "status TEXT NOT NULL DEFAULT 'draft', days INTEGER NOT NULL DEFAULT 15, "
                "cutoff_ts REAL NOT NULL DEFAULT 0, model_main TEXT DEFAULT '', "
                "model_chatroom TEXT DEFAULT '', candidate_count INTEGER NOT NULL DEFAULT 0, "
                "payload TEXT NOT NULL DEFAULT '{}', raw_response TEXT DEFAULT '', "
                "error TEXT DEFAULT '', apply_result TEXT DEFAULT '', "
                "created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, "
                "applied_at REAL, discarded_at REAL)"
            )
            await db.execute(
                "INSERT INTO daily_memory_compress_reviews "
                "(id, target, status, payload, apply_result) VALUES (?,?,?,?,?)",
                (
                    "review-1",
                    "main",
                    status,
                    json.dumps({"main": {"batches": []}, "chatroom": {"batches": []}}),
                    json.dumps(apply_result or {}),
                ),
            )
            await db.commit()

    def _connect(self):
        return aiosqlite.connect(self.db_path)

    async def test_only_first_request_can_claim_draft(self):
        import memory

        await self._create_review()
        with patch.object(memory, "get_db", self._connect), patch.object(
            memory, "_ensure_daily_compression_schema", AsyncMock()
        ):
            first_claimed, first_review = await memory._claim_daily_compression_review("review-1")
            second_claimed, second_review = await memory._claim_daily_compression_review("review-1")

        self.assertTrue(first_claimed)
        self.assertFalse(second_claimed)
        self.assertEqual(first_review["status"], "applying")
        self.assertEqual(second_review["status"], "applying")

    async def test_already_applied_review_returns_stored_result_without_writing_again(self):
        import memory

        stored = {
            "main": {"deleted": 2, "created_daily": 1, "created_important": 0},
            "chatroom": {"deleted": 0, "created_daily": 0, "created_important": 0},
        }
        await self._create_review(status="applied", apply_result=stored)
        main_apply = AsyncMock()
        chatroom_apply = AsyncMock()
        with patch.object(memory, "get_db", self._connect), patch.object(
            memory, "_ensure_daily_compression_schema", AsyncMock()
        ), patch.object(memory, "_apply_main_daily_draft", main_apply), patch.object(
            memory, "_apply_chatroom_daily_draft", chatroom_apply
        ):
            result = await memory.apply_daily_compression_review("review-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["review"]["apply_result"], stored)
        main_apply.assert_not_awaited()
        chatroom_apply.assert_not_awaited()


class DailyCompressionFrontendSafetyTests(unittest.TestCase):
    def test_main_memory_page_has_busy_guard_and_elapsed_timer(self):
        source = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")

        self.assertIn("_dailyCompressionRequestBusy", source)
        self.assertIn("startCompressionElapsedTimer", source)
        self.assertIn("clearInterval", source)
        self.assertIn("已等待", source)

    def test_chatroom_page_has_busy_guard_and_elapsed_timer(self):
        source = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")

        self.assertIn("_chatroomCompressionRequestBusy", source)
        self.assertIn("startChatroomCompressionElapsedTimer", source)
        self.assertIn("clearInterval", source)
        self.assertIn("已等待", source)


class DigestBatchingTests(unittest.TestCase):
    def test_digest_batches_stay_between_twenty_and_fifty(self):
        from memory import _split_into_groups

        for total in (51, 59, 69, 99, 101, 119, 151):
            groups = _split_into_groups(list(range(total)))
            sizes = [len(group) for group in groups]
            self.assertEqual(sum(sizes), total)
            self.assertLessEqual(max(sizes), 50, (total, sizes))
            self.assertGreaterEqual(min(sizes), 20, (total, sizes))

    def test_digest_batch_boundaries_are_fifty_and_twenty(self):
        from memory import _split_into_groups

        self.assertEqual([len(group) for group in _split_into_groups(list(range(50)))], [50])
        self.assertEqual([len(group) for group in _split_into_groups(list(range(51)))], [26, 25])
        self.assertEqual([len(group) for group in _split_into_groups(list(range(100)))], [50, 50])

    def test_digest_thresholds_and_prompt_match_new_policy(self):
        memory_source = (ROOT / "memory.py").read_text(encoding="utf-8")
        chatroom_source = (ROOT / "chatroom.py").read_text(encoding="utf-8")

        self.assertIn("min_messages=40", memory_source)
        self.assertIn("if len(msgs) < 40", chatroom_source)
        self.assertIn("至少需要 40 条", chatroom_source)
        self.assertIn("每 50 条消息通常产出 1-3 条 daily", memory_source)


if __name__ == "__main__":
    unittest.main()
