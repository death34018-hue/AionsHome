import unittest
from unittest.mock import patch

import capabilities
from context_builder import BAND_VIBRATE_CMD_PATTERN, strip_tool_commands
from routes.chat import _extract_mi_band_commands
from web_search import WebCommandStreamFilter


class MiBandAiCommandTests(unittest.TestCase):
    def test_band_note_ability_text_supports_passive_wake_up_scenarios(self):
        helper = getattr(capabilities, "build_band_note_ability_text", None)
        self.assertTrue(callable(helper))

        normal = helper("星澜")
        passive = helper("星澜", passive=True)
        for text in (normal, passive):
            self.assertIn("[BAND_NOTE_SINGLE:一句纸条]", text)
            self.assertIn("[BAND_NOTE_CALL:一句纸条]", text)
            self.assertIn("星澜", text)
        self.assertNotIn("装睡", normal)
        self.assertIn("叫醒", passive)
        self.assertIn("没有动静", passive)
        self.assertIn("装睡", passive)
        with patch.object(capabilities, "is_capability_enabled", return_value=False):
            self.assertEqual("", helper("星澜", passive=True))

    def test_extracts_supported_patterns_and_hides_protocol_text(self):
        cleaned, commands = _extract_mi_band_commands(
            "宝宝看我一下 [BAND_VIBRATE:single] 再叫你 [BAND_VIBRATE:call]"
        )
        self.assertEqual(commands, ["single", "call"])
        self.assertEqual(cleaned, "宝宝看我一下  再叫你")

    def test_tool_stripper_removes_band_commands_case_insensitively(self):
        text = strip_tool_commands("来啦 [band_vibrate:CALL]")
        self.assertEqual(text, "来啦")
        self.assertEqual(BAND_VIBRATE_CMD_PATTERN.findall("[BAND_VIBRATE:single]"), ["single"])

        note_text = strip_tool_commands(
            "正文 [band_note_single:这句不能进入TTS和上下文] 尾巴"
        )
        self.assertEqual(note_text, "正文  尾巴")

    def test_stream_filter_never_exposes_band_protocol(self):
        stream_filter = WebCommandStreamFilter()
        visible = ""
        for chunk in ("快看我 [BAND_", "VIBRATE:single", "] 宝宝"):
            visible += stream_filter.feed(chunk)
        visible += stream_filter.flush()
        self.assertEqual("快看我  宝宝", visible)

        note_filter = WebCommandStreamFilter()
        visible = ""
        for chunk in ("正文 [BAND_NOTE_", "CALL:绝密纸条", "] 尾巴"):
            visible += note_filter.feed(chunk)
        visible += note_filter.flush()
        self.assertEqual("正文  尾巴", visible)


if __name__ == "__main__":
    unittest.main()
