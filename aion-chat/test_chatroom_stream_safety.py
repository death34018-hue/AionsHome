import asyncio
import unittest

from routes.chatroom import _consume_chatroom_realtime_stream, _consume_chatroom_stream


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
    async def test_connor_safe_live_failure_resets_bubble_then_uses_legacy_once(self):
        queue = asyncio.Queue()
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Chunks(["安全正文。" * 8, "�" * 8])
            return _Chunks(["旧管线完整回复"])

        outcome = await _consume_chatroom_realtime_stream(
            source_factory,
            queue,
            chunk_type="connor_chunk",
            transport_mode="safe_live",
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(attempts, 2)
        self.assertTrue(outcome.used_fallback)
        self.assertFalse(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "旧管线完整回复")
        self.assertIn({"type": "connor_reset"}, events)

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

    async def test_aion_safe_live_failure_uses_aion_reset_event(self):
        queue = asyncio.Queue()
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Chunks(["临时正文。" * 8, "�" * 8])
            return _Chunks(["完整回复"])

        outcome = await _consume_chatroom_realtime_stream(
            source_factory,
            queue,
            chunk_type="aion_chunk",
            transport_mode="safe_live",
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertTrue(outcome.used_fallback)
        self.assertIn({"type": "aion_reset"}, events)


if __name__ == "__main__":
    unittest.main()
