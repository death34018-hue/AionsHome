import unittest
import time
from unittest.mock import AsyncMock, patch

from ambient_timeline import format_timeline


class AmbientTimelineTests(unittest.TestCase):
    def test_recent_events_keep_timestamps_and_sound_labels(self):
        now = 1_800_000_000_000
        events = [
            {"kind": "cough", "start": now - 301000, "end": now - 300001},
            {"kind": "humming", "start": now - 6000, "end": now - 3000},
            {"kind": "speech", "start": now - 2000, "end": now - 1000, "text": "盐放多了"},
            {"kind": "unknown", "start": now - 1000, "end": now},
        ]
        text = format_timeline(events, now_ms=now, utc_offset_minutes=480)
        self.assertNotIn("咳嗽", text)
        self.assertIn("哼歌", text)
        self.assertIn("说：盐放多了", text)
        self.assertRegex(text, r"\d{2}:\d{2}:\d{2}–\d{2}:\d{2}:\d{2} 哼歌")
        self.assertEqual(len(text.splitlines()), 2)

    def test_bad_times_and_future_events_are_ignored(self):
        now = 1_800_000_000_000
        self.assertEqual(format_timeline([
            {"kind": "speech", "start": float("nan"), "end": now, "text": "x"},
            {"kind": "cough", "start": now + 6000, "end": now + 7000},
        ], now_ms=now), "")


class AmbientRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sound_only_request_reaches_sentinel_and_reply_context_keeps_timeline(self):
        from routes import chatroom as route
        now = time.time() * 1000
        body = route.AmbientVoiceEvaluate(events=[
            {"kind": "humming", "start": now - 5000, "end": now - 1000}
        ], listener_client_id="phone")
        judge = AsyncMock(return_value={"should_wake": False, "reason": "安静陪伴"})
        with patch.object(route, "load_chatroom_config", return_value={"ambient_voice_enabled": True}), \
             patch.object(route, "_ambient_listener_state", return_value={"active": True, "client_id": "phone"}), \
             patch.object(route, "_load_room_and_messages", AsyncMock(return_value=({"type": "group"}, []))), \
             patch.object(route, "_judge_ambient_voice", judge), \
             patch.object(route, "_emit_ambient_voice_debug", AsyncMock(return_value={})), \
             patch.object(route, "_AMBIENT_LAST_TRIGGER_BY_ROOM", {}):
            result = await route.ambient_voice_evaluate("ambient-test", body)
        self.assertFalse(result["triggered"])
        self.assertEqual(result["decision"]["reason"], "安静陪伴")
        self.assertEqual(judge.call_args.args[0], "")
        self.assertIn("哼歌", judge.call_args.args[1])
        prompt = route._ambient_voice_prompt(result["decision"], forced=False)
        self.assertIn("最近五分钟", prompt)
        self.assertIn("哼歌", prompt)

    async def test_sentinel_prompt_receives_the_timestamped_context(self):
        from routes import chatroom as route
        timeline = "21:00:02 咳嗽\n21:00:12 哼歌\n21:00:30 说：盐放多了"
        call = AsyncMock(return_value='{"should_wake":false}')
        with patch.object(route, "get_sentinel_config", return_value={"ready": True}), \
             patch("memory._call_sentinel_text", call):
            await route._judge_ambient_voice("盐放多了", timeline)
        self.assertIn(timeline, call.call_args.args[1])
