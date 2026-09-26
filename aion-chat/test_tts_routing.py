import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from ws import ConnectionManager
from tts import TTSStreamer
from generation_control import Generation


class TTSRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ConnectionManager()
        self.phone = AsyncMock()
        self.desktop = AsyncMock()
        for client, client_id, active_at in (
            (self.phone, "phone", 20), (self.desktop, "desktop", 10)
        ):
            await self.manager.connect(client)
            self.manager.register_client_id(client, client_id)
            self.manager.set_tts_state(client, True, "voice", active_at=active_at)

    async def emit(self, kind, msg_id="message", seq=None):
        data = {"msg_id": msg_id}
        if seq is not None:
            data["seq"] = seq
        await self.manager.send_tts_event({"type": kind, "data": data})

    async def test_focus_change_keeps_chunks_and_done_on_same_device(self):
        # Synthesis may finish seq 1 before seq 0.
        await self.emit("tts_chunk", seq=1)
        self.manager.set_tts_state(self.desktop, True, "voice", active_at=30)
        await self.emit("tts_chunk", seq=0)
        await self.emit("tts_done")
        events = [json.loads(call.args[0]) for call in self.phone.send_text.call_args_list]
        self.assertEqual([e["type"] for e in events], ["tts_chunk", "tts_chunk", "tts_done"])
        self.assertTrue(all(e["data"]["target_client_id"] == "phone" for e in events))
        self.desktop.send_text.assert_not_awaited()
        await self.emit("tts_chunk", msg_id="next", seq=0)
        self.desktop.send_text.assert_awaited_once()

    async def test_disconnected_owner_falls_back_to_available_device(self):
        await self.emit("tts_chunk", seq=0)
        self.manager.disconnect(self.phone)
        await self.emit("tts_chunk", seq=1)
        await self.emit("tts_done")
        self.assertEqual(self.desktop.send_text.await_count, 2)

    async def test_app_switch_between_speakers_keeps_the_whole_turn_on_one_queue(self):
        service = AsyncMock()
        await self.manager.connect(service)
        self.manager.register_client_id(service, "android-push:phone")
        self.manager.set_tts_state(service, True, "voice", can_play=False, active_at=10)

        async def reply_turn():
            await self.emit("tts_chunk", "speaker-one", 0)
            # All audio has been synthesized, but the phone is still playing it.
            await self.emit("tts_done", "speaker-one")
            self.manager.set_tts_state(service, True, "voice", active_at=30)
            await self.emit("tts_chunk", "speaker-two", 0)
            await self.emit("tts_done", "speaker-two")

        await Generation("chatroom", "room", "turn-one").start(reply_turn())
        events = [json.loads(call.args[0]) for call in self.phone.send_text.call_args_list]
        self.assertEqual([event["data"]["msg_id"] for event in events],
                         ["speaker-one", "speaker-one", "speaker-two", "speaker-two"])
        service.send_text.assert_not_awaited()

        # A later turn can select the now-active background service normally.
        await Generation("chatroom", "room", "turn-two").start(
            self.emit("tts_chunk", "later-message", 0))
        self.assertEqual(json.loads(service.send_text.call_args.args[0])["data"]["msg_id"],
                         "later-message")

    async def test_return_to_app_keeps_second_speaker_behind_background_speech(self):
        service = AsyncMock()
        await self.manager.connect(service)
        self.manager.register_client_id(service, "android-push:phone")
        self.manager.set_tts_state(service, True, "voice", active_at=30)

        async def reply_turn():
            await self.emit("tts_chunk", "speaker-one", 0)
            await self.emit("tts_done", "speaker-one")
            # Touching the page can make it newer than the draining service.
            self.manager.set_tts_state(self.phone, True, "voice", active_at=40)
            await self.emit("tts_chunk", "speaker-two", 0)
            await self.emit("tts_done", "speaker-two")

        await Generation("chatroom", "room", "return-turn").start(reply_turn())
        events = [json.loads(call.args[0]) for call in service.send_text.call_args_list]
        self.assertEqual([event["data"]["msg_id"] for event in events],
                         ["speaker-one", "speaker-one", "speaker-two", "speaker-two"])
        self.phone.send_text.assert_not_awaited()

    async def test_turn_owner_disconnect_allows_second_speaker_to_fall_back(self):
        async def reply_turn():
            await self.emit("tts_chunk", "speaker-one", 0)
            await self.emit("tts_done", "speaker-one")
            self.manager.disconnect(self.phone)
            await self.emit("tts_chunk", "speaker-two", 0)
            await self.emit("tts_done", "speaker-two")

        await Generation("chatroom", "room", "disconnect-turn").start(reply_turn())
        events = [json.loads(call.args[0]) for call in self.desktop.send_text.call_args_list]
        self.assertEqual([event["type"] for event in events], ["tts_chunk", "tts_done"])

    async def test_failed_owner_falls_back_to_available_device(self):
        await self.emit("tts_chunk", seq=0)
        self.phone.send_text.side_effect = RuntimeError("connection lost")
        await self.emit("tts_done")
        self.desktop.send_text.assert_awaited_once()

    async def test_sse_copy_uses_the_same_target_as_websocket(self):
        queue = asyncio.Queue()
        streamer = TTSStreamer("message", "voice", self.manager, sse_queue=queue)
        await streamer._notify({"type": "tts_chunk", "data": {"msg_id": "message", "seq": 0}})
        ws_event = json.loads(self.phone.send_text.call_args.args[0])
        self.assertEqual(await queue.get(), ws_event)
        self.assertEqual(ws_event["data"]["target_client_id"], "phone")

    async def test_proactive_speech_reaches_native_service_without_page_connections(self):
        self.manager.disconnect(self.phone)
        self.manager.disconnect(self.desktop)
        service = AsyncMock()
        await self.manager.connect(service)
        self.manager.register_client_id(service, "android-push:phone")
        self.manager.set_tts_state(service, True, "voice", can_play=True, active_at=30)
        for msg_id in ("msg_checkpoint_sa", "msg_sentinel_cr", "msg_monitor_sm"):
            streamer = TTSStreamer(msg_id, "voice", self.manager)
            await streamer._notify({"type": "tts_chunk", "data": {"msg_id": msg_id, "seq": 0}})
            await streamer._notify({"type": "tts_done", "data": {"msg_id": msg_id}})
        events = [json.loads(call.args[0]) for call in service.send_text.call_args_list]
        self.assertEqual(len(events), 6)
        self.assertTrue(all(event["data"]["target_client_id"] == "android-push:phone" for event in events))
