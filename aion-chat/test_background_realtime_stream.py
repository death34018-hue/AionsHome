import unittest
from unittest.mock import patch

from schedule import _consume_background_realtime_stream


class _Chunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


class _TTS:
    def __init__(self):
        self.text = []
        self.has_emitted_audio = False
        self.accepted_segment_count = 0

    async def feed_async(self, text):
        self.text.append(text)

    async def rewind_before_audio(self):
        self.text.clear()
        return True

    def cancel(self):
        pass


class BackgroundRealtimeStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_safe_wakeup_uses_same_transport_dispatcher(self):
        tts = _TTS()
        with patch("schedule.resolve_model_transport_mode", return_value="safe_live"):
            outcome = await _consume_background_realtime_stream(
                lambda: _Chunks(["及时回复。" + "呀" * 24]),
                model_key="Codex-Sol",
                tts_streamer=tts,
            )

        self.assertIsNone(outcome.result.stop_reason)
        self.assertEqual(outcome.result.committed_text, "及时回复。" + "呀" * 24)
        self.assertTrue("".join(tts.text).startswith("及时回复。"))

    async def test_failed_safe_and_legacy_wakeup_returns_no_partial_text(self):
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            return _Chunks(["部分正文。" * 8, "�" * 8])

        with patch("schedule.resolve_model_transport_mode", return_value="safe_live"):
            outcome = await _consume_background_realtime_stream(
                source_factory,
                model_key="Codex-Sol",
            )

        self.assertEqual(attempts, 2)
        self.assertTrue(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "")


if __name__ == "__main__":
    unittest.main()
