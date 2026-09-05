import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))

from routes import chatroom


class ChatroomActiveMemorySearchTest(unittest.TestCase):
    def test_tool_only_reply_is_detected_only_when_enabled(self):
        with patch.object(chatroom, "is_capability_enabled", return_value=True):
            requests = chatroom._chatroom_memory_search_requests(
                "[MEMORY_SEARCH:过敏药|latest]"
            )
        self.assertEqual("过敏药", requests[0].query)

        with patch.object(chatroom, "is_capability_enabled", return_value=False):
            self.assertEqual(
                [],
                chatroom._chatroom_memory_search_requests("[MEMORY_SEARCH:过敏药|latest]"),
            )

    def test_natural_preface_and_full_width_command_are_accepted(self):
        with patch.object(chatroom, "is_capability_enabled", return_value=True):
            requests = chatroom._chatroom_memory_search_requests(
                "我去旧账本里找找。【MEMORY_SEARCH：辣椒炒肉|latest】"
            )
        self.assertEqual(["辣椒炒肉"], [item.query for item in requests])

    def test_actor_binding_cannot_be_selected_by_model(self):
        self.assertEqual("aion", chatroom._memory_actor_for_speaker("aion"))
        self.assertEqual("connor", chatroom._memory_actor_for_speaker("connor"))
        self.assertEqual("aion", chatroom._memory_actor_for_speaker("unexpected"))

    def test_status_uses_configured_speaker_name(self):
        with patch.object(chatroom, "_name_for_identity", return_value="星野"):
            self.assertEqual("星野正在翻找记忆……", chatroom._chatroom_memory_status_text("aion"))

    def test_completed_status_uses_configured_speaker_name(self):
        with patch.object(chatroom, "_name_for_identity", return_value="星野"):
            self.assertEqual("星野翻找了记忆", chatroom._chatroom_memory_completed_text("aion"))


class ChatroomActiveMemoryCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_response_creates_status_and_followup_payload_only(self):
        queue = __import__("asyncio").Queue()
        save_status = AsyncMock(return_value={"id": "system-memory-search"})
        with (
            patch.object(chatroom, "is_capability_enabled", return_value=True),
            patch.object(chatroom, "_chatroom_sys_msg", save_status),
            patch.object(chatroom, "_name_for_identity", return_value="星野"),
        ):
            clean, triggered = await chatroom._process_chatroom_commands(
                "[MEMORY_SEARCH:开斯婷|latest]",
                "room-1",
                "connor",
                "unsaved-tool-reply",
                queue,
                user_text="上一次吃药是什么时候？",
            )
        self.assertEqual("", clean)
        self.assertEqual("开斯婷", triggered["memory_search"]["requests"][0].query)
        self.assertEqual("上一次吃药是什么时候？", triggered["memory_search"]["original_question"])
        self.assertEqual("system-memory-search", triggered["memory_search"]["system_msg_id"])
        save_status.assert_awaited_once_with("room-1", "星野正在翻找记忆……", queue)

    async def test_natural_preface_is_returned_for_normal_message_persistence(self):
        queue = __import__("asyncio").Queue()
        save_status = AsyncMock(return_value={"id": "system-memory-search"})
        with (
            patch.object(chatroom, "is_capability_enabled", return_value=True),
            patch.object(chatroom, "_chatroom_sys_msg", save_status),
            patch.object(chatroom, "_name_for_identity", return_value="星野"),
        ):
            clean, triggered = await chatroom._process_chatroom_commands(
                "等我去翻翻。【MEMORY_SEARCH：辣椒炒肉|latest】",
                "room-1", "aion", "preface-message", queue,
                user_text="上一次是什么时候？",
            )
        self.assertEqual("等我去翻翻。", clean)
        self.assertIn("memory_search", triggered)

    async def test_followup_dispatch_does_not_depend_on_preface_text(self):
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()

        payload = {
            "memory_search": {
                "requests": [], "original_question": "什么时候？",
            }
        }
        with patch.object(chatroom.asyncio, "create_task", side_effect=capture):
            chatroom._fire_chatroom_followups(
                payload, "room-1", "aion", "model-1", "preface-message"
            )
        self.assertEqual(1, len(scheduled))

    async def test_completion_rewrites_existing_status_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "chatroom-status.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE chatroom_messages (id TEXT, room_id TEXT, sender TEXT, content TEXT, created_at REAL, attachments TEXT)"
                )
                await db.execute(
                    "INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)",
                    ("status-1", "room-1", "system", "星野正在翻找记忆……", 1.0, "[]"),
                )
                await db.commit()

            broadcast = AsyncMock()
            with (
                patch.object(chatroom, "get_db", side_effect=lambda: aiosqlite.connect(db_path)),
                patch.object(chatroom, "broadcast_synced", broadcast),
                patch.object(chatroom, "_name_for_identity", return_value="星野"),
            ):
                await chatroom._complete_chatroom_memory_search_status(
                    "room-1", "status-1", "aion"
                )

            async with aiosqlite.connect(db_path) as db:
                row = await (await db.execute(
                    "SELECT content FROM chatroom_messages WHERE id='status-1'"
                )).fetchone()
            self.assertEqual("星野翻找了记忆", row[0])
            self.assertEqual("chatroom_msg_updated", broadcast.await_args.args[1]["type"])


if __name__ == "__main__":
    unittest.main()
