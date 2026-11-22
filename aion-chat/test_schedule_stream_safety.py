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
    async def test_peer_wake_preserves_public_spacing_and_multiple_tool_rounds(self):
        from ai_providers import CLI_STATUS_PREFIX
        from peer_dm_wake import WakePeerReply

        reply = WakePeerReply()
        pieces = []
        for chunks in (
            ["Hello[伙", CLI_STATUS_PREFIX + "progress", "伴私聊:first]", "\nworld"],
            ["!", "[伙伴私聊：sec", CLI_STATUS_PREFIX + "still thinking", "ond]"],
            [" Public ending."],
        ):
            pieces.extend([text async for text in reply.public_stream(_Chunks(chunks))])
        self.assertEqual("Hello\nworld! Public ending.", "".join(pieces))
        self.assertEqual("first\nsecond", reply.private_body)

    async def test_peer_wake_preserves_diagnostics_while_speech_and_cards_use_public_text(self):
        from chat_web_cards import prose_chunks
        from peer_dm_wake import WakePeerReply
        from schedule import _broadcast_trigger_debug, _consume_background_realtime_stream

        private = "她睡着了。[WEB]<b>留给你</b>[/WEB]"
        for public in ("", "晚安。"):
            for live in (False, True):
                with self.subTest(public=public, live=live):
                    cards, tts, reply = [], _TTSProbe(), WakePeerReply()
                    source = _Chunks(list(public + f"[伙伴私聊：{private}]"))
                    stream = prose_chunks(reply.public_stream(source), cards, enabled=True)
                    if live:
                        result = (await _consume_background_realtime_stream(
                            lambda: stream, model_key="test", tts_streamer=tts,
                        )).result
                    else:
                        result = await _consume_background_stream(stream, WebCommandStreamFilter(), tts)
                    meta = {"system_log_id": "wake-call", "total_tokens": 201, "reasoning_content": private}
                    prompt = [{"role": "user", "content": private, "_timeline_source": "peer_dm"}]
                    with patch("peer_dm_wake.start_peer_dm", new_callable=AsyncMock) as deliver, \
                         patch("schedule.manager.broadcast", new_callable=AsyncMock) as broadcast:
                        await reply.dispatch("connor", "patrol:1:connor", result, meta)
                        await _broadcast_trigger_debug(msg_id="test", model_key="test", usage_meta=meta, prompt_messages=prompt)
                    self.assertEqual(public, result.committed_text)
                    self.assertEqual(public, "".join(tts.parts))
                    self.assertEqual([], cards)
                    self.assertNotIn("reasoning_content", meta)
                    deliver.assert_awaited_once_with("connor", private, trigger_id="patrol:1:connor")
                    data = broadcast.await_args.args[0]["data"]
                    self.assertEqual(data["usage"]["system_log_id"], "wake-call")
                    self.assertEqual(data["usage"]["total_tokens"], 201)
                    self.assertEqual(data["prompt_messages"], prompt)

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
