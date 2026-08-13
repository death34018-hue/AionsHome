import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes.moments import _parse_moment_reply_result
from autonomy import _save_private_message


class MomentChatFollowupParsingTests(unittest.TestCase):
    def test_parses_json_comment_and_chat_followup(self):
        raw = (
            '```json\n'
            '{"comment":"拍得真好看。","send_chat_message":true,'
            '"chat_message":"看到你发的晚霞了，今天下班路上心情很好吧？"}'
            '\n```'
        )

        comment, should_send, chat_message = _parse_moment_reply_result(
            raw,
            expect_chat_decision=True,
        )

        self.assertEqual(comment, "拍得真好看。")
        self.assertTrue(should_send)
        self.assertEqual(chat_message, "看到你发的晚霞了，今天下班路上心情很好吧？")

    def test_true_without_chat_message_is_not_sent(self):
        comment, should_send, chat_message = _parse_moment_reply_result(
            '{"comment":"收到。","send_chat_message":true,"chat_message":""}',
            expect_chat_decision=True,
        )

        self.assertEqual(comment, "收到。")
        self.assertFalse(should_send)
        self.assertEqual(chat_message, "")

    def test_invalid_json_falls_back_to_plain_comment(self):
        comment, should_send, chat_message = _parse_moment_reply_result(
            "这也太可爱了吧。",
            expect_chat_decision=True,
        )

        self.assertEqual(comment, "这也太可爱了吧。")
        self.assertFalse(should_send)
        self.assertEqual(chat_message, "")

    def test_old_plain_text_path_is_unchanged(self):
        comment, should_send, chat_message = _parse_moment_reply_result(
            "普通评论",
            expect_chat_decision=False,
        )

        self.assertEqual(comment, "普通评论")
        self.assertFalse(should_send)
        self.assertEqual(chat_message, "")


class LastActiveChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_connor_followup_uses_last_active_room(self):
        save_msg = AsyncMock(return_value={"id": "cm_test"})
        with (
            patch("autonomy.manager.get_connor_last_active", return_value="room_active"),
            patch("autonomy._latest_connor_room_id", new=AsyncMock(return_value="room_latest")),
            patch("routes.chatroom._save_msg", save_msg),
        ):
            result = await _save_private_message("connor", "我看到你的朋友圈了。")

        self.assertEqual(result, {"id": "cm_test"})
        save_msg.assert_awaited_once_with(
            "room_active",
            "connor",
            "我看到你的朋友圈了。",
            attachments=[],
            auto_tts=True,
        )

    async def test_connor_structured_report_can_disable_tts(self):
        save_msg = AsyncMock(return_value={"id": "cm_report"})
        attachment = {"type": "lounge_visit_report"}
        with (
            patch("autonomy.manager.get_connor_last_active", return_value="room_active"),
            patch("autonomy._latest_connor_room_id", new=AsyncMock(return_value="room_latest")),
            patch("routes.chatroom._save_msg", save_msg),
        ):
            result = await _save_private_message(
                "connor",
                "刚才接待了一位朋友。",
                [attachment],
                auto_tts=False,
            )

        self.assertEqual(result, {"id": "cm_report"})
        save_msg.assert_awaited_once_with(
            "room_active",
            "connor",
            "刚才接待了一位朋友。",
            attachments=[attachment],
            auto_tts=False,
        )


if __name__ == "__main__":
    unittest.main()
