import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_mode import (
    active_wechat_modes_for_route,
    find_wechat_mode_for_sender,
    parse_wechat_mode_command,
    public_wechat_modes,
    render_wechat_bubbles,
    set_wechat_mode,
)


class WeChatModeStateTests(unittest.TestCase):
    def test_exact_control_commands_ignore_outer_whitespace(self):
        self.assertEqual(parse_wechat_mode_command(" \n[微信模式开启]\t"), "enable")
        self.assertEqual(parse_wechat_mode_command("[微信模式关闭]"), "disable")
        self.assertEqual(parse_wechat_mode_command("［微信模式开启］"), "enable")
        self.assertEqual(parse_wechat_mode_command("［微信模式关闭］"), "disable")
        self.assertEqual(parse_wechat_mode_command("请[微信模式开启]"), "")

    def test_enabled_mode_persists_and_matches_both_routes(self):
        settings = {}

        mode = set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="peer-1",
            inbound_route={"source_type": "chatroom", "source_id": "room-1"},
            outbound_routes=[
                {"source_type": "chatroom", "source_id": "room-1"},
                {"source_type": "aion_private", "source_id": "conv-1"},
            ],
            enabled=True,
            now=100,
        )

        self.assertTrue(mode["enabled"])
        self.assertEqual(
            find_wechat_mode_for_sender(settings, "bot-1", "peer-1")["inbound_route"],
            {"source_type": "chatroom", "source_id": "room-1"},
        )
        self.assertEqual(
            len(active_wechat_modes_for_route(settings, "aion_private", "conv-1")),
            1,
        )

    def test_disabling_mode_keeps_routes_but_removes_active_match(self):
        settings = {}
        set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="peer-1",
            inbound_route={"source_type": "chatroom", "source_id": "room-1"},
            outbound_routes=[{"source_type": "chatroom", "source_id": "room-1"}],
            enabled=True,
            now=100,
        )

        disabled = set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="peer-1",
            inbound_route={"source_type": "chatroom", "source_id": "room-1"},
            outbound_routes=[{"source_type": "chatroom", "source_id": "room-1"}],
            enabled=False,
            now=200,
        )

        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["inbound_route"]["source_id"], "room-1")
        self.assertEqual(active_wechat_modes_for_route(settings, "chatroom", "room-1"), [])

    def test_public_modes_are_sanitized_and_routes_are_deduplicated(self):
        settings = {}
        set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="peer-1",
            inbound_route={"source_type": "private", "source_id": "conv-1"},
            outbound_routes=[
                {"source_type": "private", "source_id": "conv-1"},
                {"source_type": "aion_private", "source_id": "conv-1"},
            ],
            enabled=True,
            now=100,
        )

        rows = public_wechat_modes(settings)

        self.assertEqual(rows[0]["outbound_routes"], [
            {"source_type": "aion_private", "source_id": "conv-1"},
        ])
        self.assertNotIn("context_token", rows[0])


class WeChatBubbleRendererTests(unittest.TestCase):
    def test_plain_lines_and_inner_monologue_become_separate_messages(self):
        event = {
            "type": "msg_created",
            "data": {
                "id": "msg-1",
                "role": "assistant",
                "content": "第一句。\n第二句。[心里嘀咕：其实很想她]",
                "attachments": [],
                "reasoning_content": "must never leak",
            },
        }

        self.assertEqual(
            render_wechat_bubbles(event, source_label="私聊", sender_name="Companion"),
            [
                "Companion：第一句。",
                "Companion：第二句。",
                "Companion：💭心里嘀咕：其实很想她",
            ],
        )

    def test_structured_markdown_stays_together(self):
        event = {
            "type": "msg_created",
            "data": {
                "id": "msg-2",
                "role": "assistant",
                "content": "清单：\n- 第一项\n- 第二项",
                "attachments": [],
            },
        }

        self.assertEqual(
            render_wechat_bubbles(event, source_label="私聊", sender_name="Companion"),
            ["Companion：清单：\n- 第一项\n- 第二项"],
        )

    def test_music_attachment_is_a_message_when_text_is_empty(self):
        event = {
            "type": "msg_created",
            "data": {
                "id": "msg-3",
                "role": "assistant",
                "content": "",
                "attachments": [
                    {"type": "music", "name": "夜曲", "artist": "歌手"},
                ],
            },
        }

        self.assertEqual(
            render_wechat_bubbles(event, source_label="私聊", sender_name="Companion"),
            ["Companion：🎵 《夜曲》— 歌手"],
        )

    def test_tool_markers_meta_and_reasoning_never_leak(self):
        event = {
            "type": "msg_created",
            "data": {
                "id": "msg-4",
                "role": "assistant",
                "content": "正文[MUSIC:夜曲]<meta>发送时间：秘密</meta>",
                "attachments": [],
                "reasoning_content": "隐藏推理",
            },
        }

        rendered = "\n".join(
            render_wechat_bubbles(event, source_label="群聊", sender_name="Companion")
        )

        self.assertEqual(rendered, "Companion：正文")
        self.assertNotIn("群聊", rendered)

    def test_overflow_is_merged_without_dropping_text(self):
        event = {
            "type": "msg_created",
            "data": {
                "id": "msg-5",
                "role": "assistant",
                "content": "一\n二\n三\n四",
                "attachments": [],
            },
        }

        self.assertEqual(
            render_wechat_bubbles(
                event,
                source_label="私聊",
                sender_name="Companion",
                max_bubbles=3,
            ),
            [
                "Companion：一",
                "Companion：二",
                "Companion：三\n四",
            ],
        )


if __name__ == "__main__":
    unittest.main()
