import os
import tempfile
import time
import unittest
from unittest.mock import patch

import aiosqlite

import autonomy_state
from tts import _has_unclosed_tag, _strip_tags
from sync_events import sanitize_sync_event


class AutonomyReplyMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_patch = patch.object(autonomy_state, "DB_PATH", self.path)
        self.db_patch.start()
        async with aiosqlite.connect(self.path) as db:
            await autonomy_state.ensure_autonomy_tables(db)

    async def asyncTearDown(self):
        self.db_patch.stop()
        os.unlink(self.path)

    async def test_disabled_actor_injection_leaves_messages_unchanged(self):
        messages = [{"role": "user", "content": "你好"}]

        result = await autonomy_state.inject_autonomy_ability(messages, "aion")

        self.assertIs(messages, result)
        self.assertEqual([{"role": "user", "content": "你好"}], messages)

    async def test_enabled_actor_still_gets_no_state_packet_prompt(self):
        await autonomy_state.update_actor_config("connor", enabled=True)
        messages = [{"role": "user", "content": "你好"}]

        self.assertEqual("", await autonomy_state.autonomy_prompt_text("connor"))
        self.assertIs(messages, await autonomy_state.inject_autonomy_ability(messages, "connor"))
        self.assertEqual([{"role": "user", "content": "你好"}], messages)

    async def test_legacy_private_reply_tag_is_cleaned_without_recording_packet(self):
        await autonomy_state.update_actor_config("aion", enabled=True)
        event = {
            "type": "msg_created",
            "data": {
                "id": "m1", "role": "assistant", "content":
                '正文<autonomy_state>{"state":"惦记她","next":{"after_minutes":30}}</autonomy_state>',
                "created_at": time.time(),
            },
        }

        cleaned, changed = await autonomy_state.process_persona_message_event(event)

        self.assertFalse(changed)
        self.assertEqual("正文", cleaned["data"]["content"])
        self.assertIsNone(await autonomy_state.get_current_packet("aion"))
        self.assertIsNone(await autonomy_state.get_current_packet("connor"))

    async def test_legacy_chatroom_tag_is_cleaned_without_recording_packet(self):
        await autonomy_state.update_actor_config("connor", enabled=True)
        event = {
            "type": "chatroom_msg_created",
            "data": {
                "id": "c1", "sender": "connor", "content":
                '收到<autonomy_state>{"state":"想出去","next":{"after_minutes":60}}</autonomy_state>',
                "created_at": time.time(),
            },
        }

        cleaned, changed = await autonomy_state.process_persona_message_event(event)

        self.assertFalse(changed)
        self.assertEqual("收到", cleaned["data"]["content"])
        self.assertIsNone(await autonomy_state.get_current_packet("connor"))
        self.assertIsNone(await autonomy_state.get_current_packet("aion"))

    async def test_malformed_legacy_block_keeps_body_without_creating_fallback(self):
        await autonomy_state.update_actor_config(
            "aion", enabled=True, min_interval_minutes=20, max_interval_minutes=40
        )
        event = {
            "type": "msg_created",
            "data": {
                "id": "m2", "role": "assistant",
                "content": "别忘了锅<autonomy_state>{坏掉了}</autonomy_state>",
                "created_at": time.time(),
            },
        }

        cleaned, changed = await autonomy_state.process_persona_message_event(event)

        self.assertFalse(changed)
        self.assertEqual("别忘了锅", cleaned["data"]["content"])
        self.assertIsNone(await autonomy_state.get_current_packet("aion"))

    def test_tts_strips_autonomy_block(self):
        text = '听得到<autonomy_state>{"state":"秘密"}</autonomy_state>'
        self.assertEqual("听得到", _strip_tags(text))

    def test_tts_hides_unclosed_autonomy_block(self):
        text = '听得到。<autonomy_state>{"state":"秘密"'
        self.assertEqual("听得到。", _strip_tags(text))
        self.assertTrue(_has_unclosed_tag(text))

    async def test_autonomy_wake_does_not_create_a_state_packet(self):
        await autonomy_state.update_actor_config("aion", enabled=True)
        raw_content = '自主醒来后说的话<autonomy_state>{"state":"残缺"'
        event = {
            "type": "msg_created",
            "data": {
                "id": "wake-message", "role": "assistant", "content": raw_content,
                "created_at": time.time(),
            },
        }
        cleaned, changed = await autonomy_state.process_persona_message_event(event)

        self.assertFalse(changed)
        self.assertEqual("自主醒来后说的话", cleaned["data"]["content"])
        self.assertIsNone(await autonomy_state.get_current_packet("aion"))

    async def test_other_actor_reply_never_refreshes_a_state_packet(self):
        await autonomy_state.update_actor_config("connor", enabled=True)
        event = {
            "type": "chatroom_msg_created",
            "data": {
                "id": "connor-reply", "sender": "connor",
                "content": '收到<autonomy_state>{"state":"正在回应","next":{"after_minutes":30}}</autonomy_state>',
                "created_at": time.time(),
            },
        }
        _cleaned, changed = await autonomy_state.process_persona_message_event(event)

        self.assertFalse(changed)
        self.assertIsNone(await autonomy_state.get_current_packet("connor"))

    def test_reconnect_sync_cache_never_keeps_autonomy_block(self):
        event = {"type": "chatroom_msg_created", "data": {"content": "正文<autonomy_state>{}</autonomy_state>"}}
        clean = sanitize_sync_event(event)
        self.assertEqual("正文", clean["data"]["content"])
        self.assertIn("<autonomy_state>", event["data"]["content"])


if __name__ == "__main__":
    unittest.main()
