import asyncio
import unittest

from routes.chatroom import _consume_chatroom_stream


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


class ChatroomStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_chatroom_emits_clean_prefix_and_nonspoken_stop_notice(self):
        queue = asyncio.Queue()
        source = _Chunks([
            "Normal English 😏 与中文正文。" * 100,
            "�" * 30,
            "unreachable",
        ])

        result = await _consume_chatroom_stream(
            source,
            queue,
            chunk_type="aion_chunk",
        )
        events = []
        while not queue.empty():
            events.append(await queue.get())
        visible = "".join(event["content"] for event in events)

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertIn("Normal English", result.committed_text)
        self.assertNotIn("�", visible)
        self.assertNotIn("unreachable", visible)
        self.assertTrue(visible.endswith(f"[{result.notice}]"))


if __name__ == "__main__":
    unittest.main()
