import unittest

from schedule import _consume_background_stream
from web_search import WebCommandStreamFilter


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


class _TTSProbe:
    def __init__(self):
        self.parts = []

    async def feed_async(self, text):
        self.parts.append(text)


class ScheduleStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_tts_stops_before_protocol_dump(self):
        source = _Chunks([
            "监督回复正文。" * 100,
            'data: {"choices":[{"delta":{"content":"x"}}]}\n' * 80,
        ])
        tts = _TTSProbe()

        result = await _consume_background_stream(
            source,
            WebCommandStreamFilter(),
            tts,
        )

        spoken = "".join(tts.parts)
        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertIn("监督回复正文", spoken)
        self.assertNotIn('"choices"', spoken)


if __name__ == "__main__":
    unittest.main()
