import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
