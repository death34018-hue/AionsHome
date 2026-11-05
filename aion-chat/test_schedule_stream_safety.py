import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from schedule import _consume_background_stream, _process_background_reply_commands
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
    async def test_monitor_selfie_is_dispatched_once_and_hidden_from_history_and_voice(self):
        from routes import chatroom
        raw = '一起遛狗。\n[SELFIE: characters=anchor_legacy,朋友 | On a walk.]'
        with patch.dict('capabilities.SETTINGS', {'image_gen_enabled': True}), \
                patch.object(chatroom, '_chatroom_image_gen', new=AsyncMock()) as draw:
            visible = await _process_background_reply_commands(raw, target={'type':'chatroom', 'room_id':'room'},
                conv_id=None, sender='connor', ai_msg_id='msg')
            await asyncio.sleep(0)
            self.assertEqual(visible, '一起遛狗。')
            draw.assert_awaited_once_with('room', 'connor', 'characters=anchor_legacy,朋友 | On a walk.', True)
        for sender, expected in [('connor', visible), ('user', raw)]:
            row = chatroom._chatroom_message_dict({'sender':sender, 'content':raw, 'attachments':'[]'})
            self.assertEqual(row['content'], expected)
        command_filter = WebCommandStreamFilter()
        self.assertEqual((''.join(command_filter.feed(c) for c in raw) + command_filter.flush()).strip(), visible)

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
