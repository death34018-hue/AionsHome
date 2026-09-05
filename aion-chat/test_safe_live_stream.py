import asyncio
import unittest

from safe_live_stream import (
    SAFE_LIVE_QUARANTINE_CHARS,
    SafeLiveStreamGuard,
    consume_safe_live_stream,
)


class _Chunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class SafeLiveStreamGuardTest(unittest.TestCase):
    def test_first_twenty_four_chars_release_eight_and_keep_sixteen(self):
        guard = SafeLiveStreamGuard()

        self.assertEqual(SAFE_LIVE_QUARANTINE_CHARS, 16)
        self.assertEqual(guard.feed("一" * 24), "一" * 8)
        result = guard.finish()

        self.assertEqual(result.committed_text, "一" * 16)
        self.assertIsNone(result.stop_reason)

    def test_cross_chunk_think_marker_never_reaches_committed_text(self):
        guard = SafeLiveStreamGuard()
        visible = guard.feed("正常正文。" * 8 + "<thi")
        visible += guard.feed("nk>隐藏推理")
        result = guard.finish()

        self.assertNotIn("<thi", visible)
        self.assertEqual(result.stop_reason, "quality")

    def test_short_sse_envelope_is_rejected_before_release(self):
        guard = SafeLiveStreamGuard()

        self.assertEqual(guard.feed('data: {"choices":[{"delta":{"content":"x"}}]}'), "")
        self.assertEqual(guard.finish().stop_reason, "quality")

    def test_replacement_character_run_is_rejected(self):
        guard = SafeLiveStreamGuard()
        guard.feed("正常正文。" * 8)
        guard.feed("�" * 8)

        self.assertEqual(guard.finish().stop_reason, "quality")

    def test_split_raw_openai_envelope_never_releases_its_prefix(self):
        guard = SafeLiveStreamGuard()

        visible = guard.feed('{"choices":' + " " * 20)
        visible += guard.feed('[{"delta":{"content":"泄露"}}]}')

        self.assertEqual(visible, "")
        self.assertEqual(guard.finish().stop_reason, "quality")

    def test_unknown_angle_protocol_is_rejected(self):
        guard = SafeLiveStreamGuard()

        self.assertEqual(guard.feed("正常正文。<custom_protocol>secret"), "")
        self.assertEqual(guard.finish().stop_reason, "quality")

    def test_long_unknown_angle_protocol_is_held_across_chunks(self):
        guard = SafeLiveStreamGuard()

        visible = guard.feed('前文。<custom_protocol attribute="' + "x" * 24)
        visible += guard.feed('secret">不能显示')

        self.assertNotIn("<custom", visible)
        self.assertNotIn("secret", visible)
        self.assertEqual(guard.finish().stop_reason, "quality")

    def test_codex_error_chunk_is_not_treated_as_assistant_text(self):
        guard = SafeLiveStreamGuard()

        visible = guard.feed("已经生成的正文。" * 4)
        visible += guard.feed("[CodexCLI错误] turn failed")

        self.assertNotIn("CodexCLI错误", visible)
        self.assertEqual(guard.finish().stop_reason, "transport")

    def test_gemini_provider_errors_are_not_treated_as_assistant_text(self):
        for error_text in (
            "[Gemini错误 503] upstream unavailable",
            "[自定义中转站错误] upstream unavailable",
        ):
            with self.subTest(error_text=error_text):
                guard = SafeLiveStreamGuard()
                visible = guard.feed(error_text)

                self.assertEqual(visible, "")
                self.assertEqual(guard.finish().stop_reason, "transport")


class ConsumeSafeLiveStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_known_command_is_retained_for_final_processing_but_never_visible(self):
        source = _Chunks([
            "晚安呀。 [MEM",
            "ORY:记住午睡]",
            "做个好梦。" + "呀" * 24,
        ])
        visible = []

        result = await consume_safe_live_stream(source, visible.append)

        self.assertIsNone(result.stop_reason)
        self.assertEqual(
            result.committed_text,
            "晚安呀。 [MEMORY:记住午睡]做个好梦。" + "呀" * 24,
        )
        self.assertNotIn("MEMORY", "".join(visible))
        self.assertIn("晚安呀。", "".join(visible))
        self.assertIn("做个好梦。", "".join(visible))

    async def test_long_text_commands_over_512_chars_are_retained_but_hidden(self):
        commands = (
            "[DRAW:" + "d" * 900 + "]",
            "[SELFIE:" + "s" * 900 + "]",
            "[MEMORY:" + "m" * 900 + "]",
            "[MOMENT:" + "p" * 900 + "|false]",
            "[许愿：" + "愿" * 900 + "]",
            "[悄悄话：" + "话" * 900 + "]",
            "[微信消息：" + "信" * 900 + "]",
            "[WEB_EXTRACT:https://example.com/" + "u" * 900 + "]",
            "[REMINDER:2026-09-04|" + "r" * 900 + "]",
            "[APP_LOCK:social|10|" + "a" * 900 + "]",
            "<AUTONOMY_STATE>" + "z" * 900 + "</AUTONOMY_STATE>",
        )

        for command in commands:
            with self.subTest(command=command[:32]):
                source = _Chunks(["前文。" + command[:300], command[300:] + "后文。"])
                visible = []

                result = await consume_safe_live_stream(source, visible.append)

                self.assertIsNone(result.stop_reason)
                self.assertIn(command, result.committed_text)
                self.assertEqual("".join(visible), "前文。后文。")

    async def test_song_command_can_use_the_full_reply_budget(self):
        command = "[SONG]" + "l" * 5_000 + "[/SONG]"
        source = _Chunks(["开场。" + command[:700], command[700:] + "完成。"])
        visible = []

        result = await consume_safe_live_stream(source, visible.append)

        self.assertIsNone(result.stop_reason)
        self.assertIn(command, result.committed_text)
        self.assertEqual("".join(visible), "开场。完成。")

    async def test_oversized_short_control_command_still_stops_and_closes_source(self):
        source = _Chunks(["正常正文。" * 8, "[TOY:" + "x" * 513, "不能出现"])
        visible = []

        result = await consume_safe_live_stream(source, visible.append)

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertNotIn("不能出现", "".join(visible))

    async def test_device_and_scene_commands_stay_hidden_but_monologue_remains_visible(self):
        source = _Chunks([
            "睡吧。[拍拍抱枕:拍拍调慢][DATE_STATE:平静]",
            "[APP_LOCK:social|10|午睡][心里嘀咕：终于肯睡了]" + "呀" * 24,
        ])
        visible = []

        result = await consume_safe_live_stream(source, visible.append)
        shown = "".join(visible)

        self.assertIsNone(result.stop_reason)
        self.assertNotIn("拍拍抱枕", shown)
        self.assertNotIn("DATE_STATE", shown)
        self.assertNotIn("APP_LOCK", shown)
        self.assertIn("[心里嘀咕：终于肯睡了]", shown)

    async def test_chinese_wechat_command_never_enters_visible_text(self):
        source = _Chunks(["我去叫她。[微信消", "息：朋友|快来睡觉]" + "呀" * 24])
        visible = []

        result = await consume_safe_live_stream(source, visible.append)

        self.assertIsNone(result.stop_reason)
        self.assertNotIn("微信消息", "".join(visible))
        self.assertIn("[微信消息：朋友|快来睡觉]", result.committed_text)

    async def test_activity_keeps_stream_alive_without_becoming_text(self):
        from stream_safety import StreamActivity

        async def source():
            yield StreamActivity()
            yield "正文"

        visible = []
        result = await consume_safe_live_stream(source(), visible.append)

        self.assertEqual(result.committed_text, "正文")
        self.assertEqual("".join(visible), "正文")


if __name__ == "__main__":
    unittest.main()
