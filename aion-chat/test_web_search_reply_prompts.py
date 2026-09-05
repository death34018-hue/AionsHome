import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from routes import chatroom as chatroom_routes


def _extract_segment(source, start_marker, end_marker):
    start = source.find(start_marker)
    if start == -1:
        raise AssertionError(f"start marker not found: {start_marker!r}")

    end = source.find(end_marker, start + len(start_marker))
    if end == -1:
        raise AssertionError(f"end marker not found after {start_marker!r}: {end_marker!r}")

    return source[start:end]


class WebSearchReplyPromptTests(unittest.TestCase):
    def test_extract_segment_reports_missing_markers(self):
        with self.assertRaisesRegex(AssertionError, "start marker not found"):
            _extract_segment("content", "start", "end")
        with self.assertRaisesRegex(AssertionError, "end marker not found"):
            _extract_segment("start content", "start", "end")

    def test_all_post_search_prompts_require_concise_synthesis(self):
        root = Path(__file__).parent
        required_phrases = (
            "请先自行归纳总结搜索结果",
            "只提供与当前问题或分享主题直接相关的关键信息",
            "请像平时聊天一样自然表达",
            "不要写成搜索报告，不要逐条复述搜索结果，也不要长篇大论",
        )

        prompt_markers = {
            "routes/chat.py": ("web_prompt = (", "messages ="),
            "routes/chatroom.py": ("web_prompt = (", "messages ="),
            "autonomy.py": ("[上网冲浪搜索完成]", "reply = clean_web_command_text"),
        }

        for relative_path, markers in prompt_markers.items():
            source = (root / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                prompt_segment = _extract_segment(source, *markers)
                for phrase in required_phrases:
                    self.assertIn(phrase, prompt_segment)


class ChatroomWebSearchPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def test_connor_search_followup_receives_connor_persona_for_named_codex_model(self):
        captured_messages = []

        async def fake_connor_stream(messages, model_key):
            captured_messages.extend(messages)
            yield "简短回答"

        async def passthrough_reply_commands(text, **_kwargs):
            return text

        with (
            patch("chatroom._read_connor_persona", return_value="CONNOR_PERSONA_SENTINEL"),
            patch("config.load_worldbook", return_value={"user_name": "测试用户", "user_persona": ""}),
            patch.object(
                chatroom_routes,
                "run_web_commands",
                new=AsyncMock(return_value=["【联网搜索结果】\n测试资料"]),
            ),
            patch.object(
                chatroom_routes,
                "_load_room_and_messages",
                new=AsyncMock(return_value=({"id": "room-1"}, [])),
            ),
            patch.object(chatroom_routes, "_stream_connor_model", new=fake_connor_stream),
            patch.object(chatroom_routes, "_save_msg", new=AsyncMock()),
            patch("schedule._process_background_reply_commands", new=passthrough_reply_commands),
        ):
            await chatroom_routes._chatroom_web_search(
                "room-1",
                "connor",
                "Codex-Sol",
                {"searches": ["测试查询"], "extracts": []},
            )

        persona_messages = [
            message
            for message in captured_messages
            if "CONNOR_PERSONA_SENTINEL" in str(message.get("content") or "")
        ]
        self.assertEqual(1, len(persona_messages))


if __name__ == "__main__":
    unittest.main()
