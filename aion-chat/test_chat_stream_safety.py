import asyncio
import unittest

from routes.chat import _consume_chat_stream
from stream_safety import StreamActivity


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


class ChatStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_activity_is_not_visible_or_sent_to_tts(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([StreamActivity(), "真实正文"])

        result, visible_text = await _consume_chat_stream(
            source,
            queue,
            model_key="test-model",
            tts_streamer=tts,
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(result.committed_text, "真实正文")
        self.assertEqual(visible_text, "真实正文")
        self.assertEqual("".join(tts.parts), "真实正文")
        self.assertEqual(
            "".join(event.get("content", "") for event in events),
            "真实正文",
        )

    async def test_private_chat_tts_receives_only_validated_text(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([
            "Safe English and 中文 😏。" * 100,
            "\x01" * 20,
            "never delivered",
        ])

        result, visible_text = await _consume_chat_stream(
            source,
            queue,
            model_key="test-model",
            tts_streamer=tts,
        )

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertEqual("".join(tts.parts), result.committed_text)
        self.assertNotIn("\x01", visible_text)
        self.assertNotIn("never delivered", visible_text)
        self.assertNotIn(result.notice, "".join(tts.parts))


if __name__ == "__main__":
    unittest.main()
