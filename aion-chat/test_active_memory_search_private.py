import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))

from routes import chat


class PrivateActiveMemorySearchTest(unittest.TestCase):
    def test_detects_tool_only_reply_when_capability_enabled(self):
        with patch.object(chat, "is_capability_enabled", return_value=True):
            requests = chat._private_memory_search_requests(
                "[MEMORY_SEARCH:过敏药|latest]\n[MEMORY_SEARCH:开斯婷|latest]"
            )
        self.assertEqual(["过敏药", "开斯婷"], [item.query for item in requests])

    def test_does_not_parse_when_capability_disabled(self):
        with patch.object(chat, "is_capability_enabled", return_value=False):
            requests = chat._private_memory_search_requests("[MEMORY_SEARCH:过敏药|latest]")
        self.assertEqual([], requests)

    def test_accepts_natural_preface_with_command(self):
        with patch.object(chat, "is_capability_enabled", return_value=True):
            requests = chat._private_memory_search_requests(
                "我去翻翻看。【MEMORY_SEARCH：过敏药|latest】"
            )
        self.assertEqual(["过敏药"], [item.query for item in requests])

    def test_search_status_uses_configured_character_name(self):
        self.assertEqual(
            "星野正在翻找记忆……",
            chat._private_memory_status_text({"ai_name": "星野"}),
        )

    def test_completed_status_uses_configured_character_name(self):
        self.assertEqual(
            "星野翻找了记忆",
            chat._private_memory_completed_text({"ai_name": "星野"}),
        )


class PrivateActiveMemoryStartTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_adds_status_and_schedules_one_second_pass(self):
        import asyncio

        queue = asyncio.Queue()
        status = AsyncMock(return_value="system-memory-search")
        followup = AsyncMock()
        save_preface = AsyncMock()
        with (
            patch.object(chat, "is_capability_enabled", return_value=True),
            patch.object(chat, "_private_memory_search_sys_msg", status),
            patch.object(chat, "perform_private_memory_search", followup),
            patch.object(chat, "_save_private_memory_preface", save_preface),
        ):
            started = await chat._start_private_memory_search_if_requested(
                "等我翻翻。【MEMORY_SEARCH：Ctrl+X|relevant】",
                conv_id="conv-1",
                model_key="model-1",
                original_question="还记得那次误按吗？",
                queue=queue,
                ai_msg_id="assistant-preface",
            )
            await asyncio.sleep(0)
        self.assertTrue(started)
        save_preface.assert_awaited_once_with(
            "conv-1", "assistant-preface", "等我翻翻。"
        )
        status.assert_awaited_once_with("conv-1", after_msg_id="assistant-preface")
        followup.assert_awaited_once()
        self.assertEqual(
            "system-memory-search",
            followup.await_args.kwargs["status_msg_id"],
        )
        event = await queue.get()
        self.assertEqual("memory_search", event["type"])
        self.assertEqual(["Ctrl+X"], event["queries"])

    async def test_completion_rewrites_existing_status_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "private-status.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)"
                )
                await db.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                    ("status-1", "conv-1", "system", "星野正在翻找记忆……", 1.0, "[]"),
                )
                await db.commit()

            broadcast = AsyncMock()
            with (
                patch.object(chat, "get_db", side_effect=lambda: aiosqlite.connect(db_path)),
                patch.object(chat.manager, "broadcast", broadcast),
            ):
                await chat._complete_private_memory_search_status(
                    "conv-1", "status-1", {"ai_name": "星野"}
                )

            async with aiosqlite.connect(db_path) as db:
                row = await (await db.execute(
                    "SELECT content FROM messages WHERE id='status-1'"
                )).fetchone()
            self.assertEqual("星野翻找了记忆", row[0])
            self.assertEqual("msg_updated", broadcast.await_args.args[0]["type"])


if __name__ == "__main__":
    unittest.main()
