import asyncio
import unittest

from stream_safety import (
    CHAT_STREAM_POLICY,
    THEATER_STREAM_POLICY,
    StreamActivity,
    StreamSafetyPolicy,
    StreamSafetyGuard,
    consume_safe_stream,
)


class _AsyncChunks:
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


class _FailingAsyncChunks(_AsyncChunks):
    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise RuntimeError("provider connection closed")


class StreamSafetyGuardTest(unittest.TestCase):
    def test_chat_and_theater_timeout_policies_match_ui_contract(self):
        self.assertEqual(CHAT_STREAM_POLICY.idle_timeout, 300.0)
        self.assertEqual(CHAT_STREAM_POLICY.total_timeout, 900.0)
        self.assertEqual(CHAT_STREAM_POLICY.quarantine_chars, 120)
        self.assertEqual(THEATER_STREAM_POLICY.idle_timeout, 600.0)
        self.assertEqual(THEATER_STREAM_POLICY.total_timeout, 900.0)
        self.assertEqual(THEATER_STREAM_POLICY.quarantine_chars, 120)

    def test_normal_multilingual_markdown_code_and_emoji_pass(self):
        text = (
            "中文 English 日本語 한국어 😏🤗\n"
            "Markdown **bold** and `inline_code()`.\n"
            "```python\nprint('ok', 123)\n```"
        )
        guard = StreamSafetyGuard(CHAT_STREAM_POLICY)

        self.assertEqual(guard.feed(text), "")
        result = guard.finish()

        self.assertEqual(result.committed_text, text)
        self.assertIsNone(result.stop_reason)

    def test_short_unusual_symbols_do_not_false_positive(self):
        text = "状态：✅ | 进度：[=======>  ] 70% | ∑(x²) ≈ 42"
        guard = StreamSafetyGuard(CHAT_STREAM_POLICY)

        guard.feed(text)
        result = guard.finish()

        self.assertEqual(result.committed_text, text)
        self.assertIsNone(result.stop_reason)

    def test_clean_prefix_is_kept_and_replacement_run_is_rejected(self):
        guard = StreamSafetyGuard(CHAT_STREAM_POLICY)
        clean = "这是已经验证过的正常正文。" * 80

        committed = guard.feed(clean)
        dirty_commit = guard.feed("�" * 40)
        result = guard.finish()
        visible = committed + dirty_commit + result.committed_text

        self.assertIn("已经验证过的正常正文", visible)
        self.assertNotIn("�", visible)
        self.assertEqual(result.stop_reason, "quality")

    def test_long_repeated_protocol_dump_is_rejected(self):
        guard = StreamSafetyGuard(CHAT_STREAM_POLICY)
        clean = "正常的故事仍在继续。" * 80
        protocol_dump = (
            'data: {"choices":[{"delta":{"content":"x"}}]}\n' * 80
        )

        committed = guard.feed(clean)
        dirty_commit = guard.feed(protocol_dump)
        result = guard.finish()
        visible = committed + dirty_commit + result.committed_text

        self.assertIn("正常的故事", visible)
        self.assertNotIn('"choices"', visible)
        self.assertEqual(result.stop_reason, "quality")

    def test_chat_length_limit_stops_before_oversized_suffix(self):
        guard = StreamSafetyGuard(CHAT_STREAM_POLICY)

        committed = guard.feed("a" * 5900)
        overflow_commit = guard.feed("b" * 1000)
        result = guard.finish()
        visible = committed + overflow_commit + result.committed_text

        self.assertLessEqual(len(visible), 6000)
        self.assertNotIn("b" * 700, visible)
        self.assertEqual(result.stop_reason, "length")

    def test_theater_policy_accepts_more_than_chat_limit(self):
        guard = StreamSafetyGuard(THEATER_STREAM_POLICY)
        text = "".join(
            f"Scene {index}: The story continues with detail {index * 7}. 😏\n"
            for index in range(180)
        )

        committed = guard.feed(text)
        result = guard.finish()

        self.assertEqual(committed + result.committed_text, text)
        self.assertIsNone(result.stop_reason)


class ConsumeSafeStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_activity_resets_idle_timeout_without_entering_text(self):
        async def source():
            await asyncio.sleep(0.025)
            yield StreamActivity()
            await asyncio.sleep(0.025)
            yield "正文"

        policy = StreamSafetyPolicy(
            max_chars=100,
            total_timeout=0.2,
            idle_timeout=0.04,
            quarantine_chars=0,
        )
        commits = []

        result = await consume_safe_stream(source(), policy, commits.append)

        self.assertIsNone(result.stop_reason)
        self.assertEqual(result.committed_text, "正文")
        self.assertEqual(commits, ["正文"])

    async def test_quality_stop_closes_source_and_never_commits_dirty_suffix(self):
        source = _AsyncChunks([
            "正常正文。" * 100,
            "\x00" * 20,
            "这段绝不能出现",
        ])
        commits = []

        result = await consume_safe_stream(
            source,
            CHAT_STREAM_POLICY,
            commits.append,
        )

        self.assertTrue(source.closed)
        self.assertEqual(result.stop_reason, "quality")
        self.assertNotIn("\x00", "".join(commits))
        self.assertNotIn("绝不能出现", "".join(commits))

    async def test_transport_failure_keeps_only_previously_validated_prefix(self):
        source = _FailingAsyncChunks([
            "已经确认安全的正文。" * 100,
            "仍在隔离区的尾巴",
        ])
        commits = []

        result = await consume_safe_stream(
            source,
            CHAT_STREAM_POLICY,
            commits.append,
        )

        self.assertTrue(source.closed)
        self.assertEqual(result.stop_reason, "transport")
        self.assertIn("已经确认安全的正文", "".join(commits))
        self.assertNotIn("隔离区的尾巴", "".join(commits))
        self.assertEqual(result.diagnostic_error, "provider connection closed")


if __name__ == "__main__":
    unittest.main()
