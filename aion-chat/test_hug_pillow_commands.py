import asyncio
import contextlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite


BASE_DIR = pathlib.Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import capabilities


class HugPillowParserTests(unittest.TestCase):
    def test_extracts_all_supported_commands_in_textual_order_and_hides_them(self):
        from hug_pillow_commands import extract_hug_pillow_commands

        cleaned, commands = extract_hug_pillow_commands(
            "开始 [拍拍抱枕:拍打开关] 然后慢点 [拍拍抱枕:拍拍调慢]"
            " 再慢点 [拍拍抱枕:拍拍调慢] 最后快点 [拍拍抱枕:拍拍调快]"
        )

        self.assertEqual("开始  然后慢点  再慢点  最后快点", cleaned)
        self.assertEqual(
            ["PAT_START_STOP", "SPEED_DOWN", "SPEED_DOWN", "SPEED_UP"],
            commands,
        )

    def test_malformed_or_unknown_markers_remain_visible_and_do_not_execute(self):
        from hug_pillow_commands import extract_hug_pillow_commands

        source = "[拍拍抱枕:总开关] [拍拍抱枕：拍拍调慢] [拍拍抱枕:拍拍调慢"
        cleaned, commands = extract_hug_pillow_commands(source)

        self.assertEqual(source, cleaned)
        self.assertEqual([], commands)

    def test_system_text_describes_toggle_truthfully_and_speed_as_one_step(self):
        from hug_pillow_commands import hug_pillow_system_text

        self.assertEqual(
            "Connor按下了拍拍抱枕的“拍打开关”。",
            hug_pillow_system_text("Connor", "PAT_START_STOP"),
        )
        self.assertEqual(
            "Aion将拍拍抱枕调慢了一次。",
            hug_pillow_system_text("Aion", "SPEED_DOWN"),
        )
        self.assertEqual(
            "Aion将拍拍抱枕调快了一次。",
            hug_pillow_system_text("Aion", "SPEED_UP"),
        )


class HugPillowCapabilityTests(unittest.TestCase):
    def test_capability_is_default_disabled(self):
        item = capabilities.get_capability_def("hug_pillow")

        self.assertIsNotNone(item)
        self.assertEqual("拍拍抱枕控制", item.name)
        self.assertFalse(item.default_enabled)

    def test_enabled_capability_injects_all_three_exact_commands_together(self):
        with patch(
            "capabilities.is_capability_enabled",
            side_effect=lambda key: key == "hug_pillow",
        ):
            items = asyncio.run(capabilities.build_capability_prompt_items("Ithil"))

        joined = "\n".join(items)
        self.assertIn("[拍拍抱枕:拍打开关]", joined)
        self.assertIn("[拍拍抱枕:拍拍调慢]", joined)
        self.assertIn("[拍拍抱枕:拍拍调快]", joined)
        self.assertIn("不要重复发送同一操作", joined)

    def test_disabled_capability_injects_none_of_the_commands(self):
        with patch("capabilities.is_capability_enabled", return_value=False):
            items = asyncio.run(capabilities.build_capability_prompt_items("Ithil"))

        joined = "\n".join(items)
        self.assertNotIn("[拍拍抱枕:", joined)


class HugPillowProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_broadcasts_one_ordered_batch_and_records_each_action(self):
        from hug_pillow_commands import process_hug_pillow_commands

        system_messages = []
        events = []

        async def save_system_message(text):
            system_messages.append(text)

        async def broadcast(event):
            events.append(event)

        cleaned = await process_hug_pillow_commands(
            "好 [拍拍抱枕:拍打开关] 慢一点 [拍拍抱枕:拍拍调慢]"
            " 再慢一点 [拍拍抱枕:拍拍调慢]",
            source_type="chatroom",
            source_id="room-1",
            source_msg_id="msg-1",
            sender="connor",
            sender_name="Connor",
            save_system_message=save_system_message,
            broadcast=broadcast,
        )

        self.assertEqual("好  慢一点  再慢一点", cleaned)
        self.assertEqual(
            [
                "Connor按下了拍拍抱枕的“拍打开关”。",
                "Connor将拍拍抱枕调慢了一次。",
                "Connor将拍拍抱枕调慢了一次。",
            ],
            system_messages,
        )
        self.assertEqual(1, len(events))
        self.assertEqual("hug_pillow_command", events[0]["type"])
        self.assertEqual(
            ["PAT_START_STOP", "SPEED_DOWN", "SPEED_DOWN"],
            events[0]["data"]["commands"],
        )
        self.assertEqual("chatroom", events[0]["data"]["source_type"])
        self.assertEqual("room-1", events[0]["data"]["source_id"])
        self.assertEqual("msg-1", events[0]["data"]["source_msg_id"])
        self.assertTrue(events[0]["data"]["id"].startswith("hug_"))

    async def test_process_does_nothing_for_text_without_exact_commands(self):
        from hug_pillow_commands import process_hug_pillow_commands

        system_messages = []
        events = []

        async def save_system_message(text):
            system_messages.append(text)

        async def broadcast(event):
            events.append(event)

        source = "只是聊天 [拍拍抱枕:总开关]"
        cleaned = await process_hug_pillow_commands(
            source,
            source_type="private",
            source_id="conv-1",
            source_msg_id="msg-2",
            sender="aion",
            sender_name="Aion",
            save_system_message=save_system_message,
            broadcast=broadcast,
        )

        self.assertEqual(source, cleaned)
        self.assertEqual([], system_messages)
        self.assertEqual([], events)


class HugPillowSystemContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_system_notice_is_persisted_as_model_visible(self):
        from context_builder import _is_model_visible_timeline_message
        from routes import chat

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = pathlib.Path(temp_dir) / "messages.db"
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE messages ("
                    "id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, "
                    "created_at REAL, attachments TEXT)"
                )
                await db.commit()

            @contextlib.asynccontextmanager
            async def test_db():
                db = await aiosqlite.connect(db_path)
                try:
                    yield db
                finally:
                    await db.close()

            with (
                patch.object(chat, "get_db", test_db),
                patch.object(chat.manager, "broadcast", AsyncMock()),
            ):
                await chat._hug_pillow_sys_msg(
                    "conv-1",
                    "Connor按下了拍拍抱枕的“拍打开关”。",
                    after_msg_id="msg-1",
                )

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT role, content, attachments FROM messages"
                )
                role, content, attachments_json = await cursor.fetchone()

        attachments = json.loads(attachments_json)
        self.assertEqual("system", role)
        self.assertEqual("Connor按下了拍拍抱枕的“拍打开关”。", content)
        self.assertIn({"type": "system_model_context"}, attachments)
        self.assertIn(
            {"type": "system_notice_order", "after_msg_id": "msg-1"},
            attachments,
        )
        self.assertTrue(
            _is_model_visible_timeline_message(
                {
                    "sender": role,
                    "content": content,
                    "attachments": attachments,
                }
            )
        )


class HugPillowTTSTests(unittest.TestCase):
    def test_tts_never_includes_hug_pillow_protocol_markers(self):
        from tts import split_text_for_tts

        parts = split_text_for_tts(
            "好呀。[拍拍抱枕:拍打开关]慢一点。[拍拍抱枕:拍拍调慢]"
            "再快一点。[拍拍抱枕:拍拍调快]",
            min_chars=1,
            max_chars=500,
        )

        self.assertEqual(["好呀。慢一点。再快一点。"], parts)

    def test_stream_filter_hides_command_even_when_prefix_arrives_in_chunks(self):
        from web_search import WebCommandStreamFilter

        stream_filter = WebCommandStreamFilter()
        visible = "".join(
            [
                stream_filter.feed("好呀。[拍拍"),
                stream_filter.feed("抱枕:拍打开关]"),
                stream_filter.feed("慢一点。"),
                stream_filter.flush(),
            ]
        )

        self.assertEqual("好呀。慢一点。", visible)


if __name__ == "__main__":
    unittest.main()
