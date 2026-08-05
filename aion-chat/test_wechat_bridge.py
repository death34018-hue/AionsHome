import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_bridge import (
    build_wechat_system_content,
    build_wechat_user_content,
    extract_wechat_messages,
    format_wechat_outbound_content,
    process_wechat_outbound_commands,
)


class WeChatBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_wechat_messages_removes_tool_markers_and_keeps_payloads(self):
        cleaned, messages = extract_wechat_messages(
            "我先在本地说一句。\n[微信消息：你看到的话回我一下]\n尾巴[微信消息:第二条]"
        )

        self.assertEqual(messages, ["你看到的话回我一下", "第二条"])
        self.assertNotIn("[微信消息", cleaned)
        self.assertEqual(cleaned, "我先在本地说一句。\n尾巴")

    def test_wechat_system_content_is_model_visible_record(self):
        self.assertEqual(
            build_wechat_system_content("  你看到的话回我一下  "),
            "本条为微信消息：你看到的话回我一下",
        )

    def test_wechat_user_content_marks_inbound_channel(self):
        self.assertEqual(
            build_wechat_user_content("  我在微信上回你啦  "),
            "本条消息来自于微信：我在微信上回你啦",
        )

    def test_format_wechat_outbound_content_prefixes_chatroom_ai_sender_name(self):
        self.assertEqual(
            format_wechat_outbound_content(
                "  你看到的话回我一下  ",
                source_type="chatroom",
                sender="connor",
                sender_label="Companion",
            ),
            "Companion：你看到的话回我一下",
        )
        self.assertEqual(
            format_wechat_outbound_content(
                "  你看到的话回我一下  ",
                source_type="aion_private",
                sender="aion",
                sender_label="Companion",
            ),
            "你看到的话回我一下",
        )

    def test_context_builder_strips_wechat_command_and_keeps_system_record(self):
        from context_builder import SYSTEM_MSG_CONTEXT_KEYWORDS, strip_tool_commands

        self.assertEqual(strip_tool_commands("正文[微信消息：来微信看我一下]"), "正文")
        self.assertIn("本条为微信消息", SYSTEM_MSG_CONTEXT_KEYWORDS)

    async def test_process_wechat_outbound_commands_records_and_dispatches(self):
        saved_system_messages = []
        record_route = AsyncMock()
        send_wechat_message = AsyncMock()

        cleaned, messages = await process_wechat_outbound_commands(
            "本地可见文本。[微信消息：你看到的话回我一下]",
            source_type="aion_private",
            source_id="conv_1",
            sender="aion",
            source_msg_id="msg_1",
            save_system_message=saved_system_messages.append,
            send_wechat_message=send_wechat_message,
            record_route=record_route,
        )

        self.assertEqual(cleaned, "本地可见文本。")
        self.assertEqual(messages, ["你看到的话回我一下"])
        self.assertEqual(saved_system_messages, ["本条为微信消息：你看到的话回我一下"])
        record_route.assert_awaited_once_with({
            "source_type": "aion_private",
            "source_id": "conv_1",
            "sender": "aion",
            "source_msg_id": "msg_1",
        })
        send_wechat_message.assert_awaited_once_with(
            content="你看到的话回我一下",
            source_type="aion_private",
            source_id="conv_1",
            sender="aion",
            source_msg_id="msg_1",
        )

    async def test_chatroom_wechat_command_records_and_sends_prefixed_name(self):
        saved_system_messages = []
        send_wechat_message = AsyncMock()

        with patch("wechat_bridge.resolve_wechat_sender_label", return_value="Companion"):
            cleaned, messages = await process_wechat_outbound_commands(
                "本地可见文本。[微信消息：你看到的话回我一下]",
                source_type="chatroom",
                source_id="room_1",
                sender="connor",
                source_msg_id="msg_1",
                save_system_message=saved_system_messages.append,
                send_wechat_message=send_wechat_message,
            )

        self.assertEqual(cleaned, "本地可见文本。")
        self.assertEqual(messages, ["你看到的话回我一下"])
        self.assertEqual(saved_system_messages, ["本条为微信消息：Companion：你看到的话回我一下"])
        send_wechat_message.assert_awaited_once_with(
            content="Companion：你看到的话回我一下",
            source_type="chatroom",
            source_id="room_1",
            sender="connor",
            source_msg_id="msg_1",
        )

    async def test_active_mode_turns_explicit_payload_into_visible_text_without_direct_send(self):
        import config
        from wechat_mode import set_wechat_mode

        settings = {}
        set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="peer-1",
            inbound_route={"source_type": "aion_private", "source_id": "conv_1"},
            outbound_routes=[{"source_type": "aion_private", "source_id": "conv_1"}],
            enabled=True,
            now=100,
        )
        saved_system_messages = []
        record_route = AsyncMock()
        send_wechat_message = AsyncMock()

        with patch.object(config, "SETTINGS", settings):
            cleaned, messages = await process_wechat_outbound_commands(
                "本地一句。[微信消息：微信一句]",
                source_type="aion_private",
                source_id="conv_1",
                sender="aion",
                source_msg_id="msg_1",
                save_system_message=saved_system_messages.append,
                send_wechat_message=send_wechat_message,
                record_route=record_route,
            )

        self.assertEqual(cleaned, "本地一句。\n微信一句")
        self.assertEqual(messages, ["微信一句"])
        self.assertEqual(saved_system_messages, [])
        send_wechat_message.assert_not_awaited()
        record_route.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
