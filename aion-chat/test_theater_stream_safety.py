import asyncio
import unittest

from routes.theater import _StreamingReasoningMeta, _consume_theater_stream
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
        self.text = []

    async def feed_async(self, chunk):
        self.text.append(chunk)


class TheaterStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_activity_is_not_visible_or_sent_to_tts(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([StreamActivity(), "剧场正文"])

        result = await _consume_theater_stream(source, queue, tts)

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(result.committed_text, "剧场正文")
        self.assertEqual("".join(tts.text), "剧场正文")
        self.assertEqual(
            "".join(
                event.get("content", "")
                for event in events
                if event.get("type") == "chunk"
            ),
            "剧场正文",
        )

    async def test_reasoning_meta_still_mirrors_deltas_live(self):
        queue = asyncio.Queue()
        meta = _StreamingReasoningMeta(queue)

        meta["reasoning_content"] = "第一段"
        meta["reasoning_content"] = "第一段\n\n第二段"

        first = await queue.get()
        second = await queue.get()
        self.assertEqual(first, {"type": "reasoning", "content": "第一段"})
        self.assertEqual(second, {"type": "reasoning", "content": "\n\n第二段"})

    async def test_dirty_suffix_is_neither_emitted_nor_sent_to_tts(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([
            "这是正常的小剧场正文。" * 100,
            "\x00" * 20,
            "不能出现的后续",
        ])

        result = await _consume_theater_stream(source, queue, tts)
        events = []
        while not queue.empty():
            events.append(await queue.get())
        visible = "".join(
            event["content"] for event in events if event["type"] == "chunk"
        )

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertNotIn("\x00", visible)
        self.assertNotIn("不能出现", visible)
        self.assertEqual("".join(tts.text), result.committed_text)
        self.assertNotIn(result.notice, "".join(tts.text))
        self.assertTrue(visible.endswith(f"[{result.notice}]"))


if __name__ == "__main__":
    unittest.main()
