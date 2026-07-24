import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from routes import chatroom
import schedule


class MiBandBackgroundCommandTest(unittest.IsolatedAsyncioTestCase):
    def test_background_tts_never_receives_raw_ai_chunks(self):
        root = Path(__file__).resolve().parent
        schedule_source = (root / "schedule.py").read_text(encoding="utf-8")
        camera_source = (root / "camera.py").read_text(encoding="utf-8")

        self.assertNotIn("alarm_tts.feed(chunk)", schedule_source)
        self.assertNotIn("monitor_tts.feed(chunk)", schedule_source)
        self.assertNotIn("core_tts.feed(chunk)", camera_source)
        self.assertNotIn("cam_tts.feed(chunk)", camera_source)
        self.assertIn("WebCommandStreamFilter", schedule_source)
        self.assertIn("WebCommandStreamFilter", camera_source)

    def test_all_independent_passive_prompts_include_band_note_ability(self):
        root = Path(__file__).resolve().parent
        schedule_source = (root / "schedule.py").read_text(encoding="utf-8")
        camera_source = (root / "camera.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(
            schedule_source.count("build_band_note_ability_text(user_name, passive=True)"),
            2,
        )
        self.assertGreaterEqual(
            camera_source.count("build_band_note_ability_text(user_name, passive=True)"),
            2,
        )
        self.assertGreaterEqual(schedule_source.count("passive_band_ability"), 4)
        self.assertGreaterEqual(camera_source.count("passive_band_messages"), 4)

    async def test_shared_background_processor_dispatches_band_command(self):
        band_processor = AsyncMock(return_value="后台叫你")
        with patch("routes.chat._process_home_commands", AsyncMock(return_value="后台叫你 [BAND_NOTE_CALL:快看我]")), \
             patch.object(schedule, "_process_background_wechat_commands", AsyncMock(return_value="后台叫你 [BAND_NOTE_CALL:快看我]")), \
             patch.object(schedule, "process_band_vibration", band_processor):
            cleaned = await schedule._process_background_reply_commands(
                "后台叫你 [BAND_NOTE_CALL:快看我]",
                target={"type": "private"},
                conv_id="conv-1",
                sender="aion",
                ai_msg_id="msg-bg",
            )

        self.assertEqual("后台叫你", cleaned)
        band_processor.assert_awaited_once_with(
            "后台叫你 [BAND_NOTE_CALL:快看我]",
            source_type="background_private",
            source_id="conv-1",
            source_msg_id="msg-bg",
            sender="aion",
        )

    async def test_chatroom_processor_dispatches_band_command(self):
        band_processor = AsyncMock(return_value="群里叫你")
        queue = asyncio.Queue()
        with patch.object(chatroom, "process_wechat_outbound_commands", AsyncMock(return_value=("群里叫你 [BAND_NOTE_SINGLE:看手环]", []))), \
             patch.object(chatroom, "process_schedule_commands", AsyncMock(side_effect=lambda text, *args, **kwargs: text)), \
             patch("routes.chat._process_home_commands", AsyncMock(side_effect=lambda text: text)), \
             patch.object(chatroom, "handle_luckin_commands", AsyncMock(side_effect=lambda text: (text, []))), \
             patch.object(chatroom, "process_band_vibration", band_processor):
            cleaned, _ = await chatroom._process_chatroom_commands(
                "群里叫你 [BAND_NOTE_SINGLE:看手环]", "room-1", "aion", "msg-room", queue
            )

        self.assertEqual("群里叫你", cleaned)
        band_processor.assert_awaited_once_with(
            "群里叫你 [BAND_NOTE_SINGLE:看手环]",
            source_type="chatroom",
            source_id="room-1",
            source_msg_id="msg-room",
            sender="aion",
        )


if __name__ == "__main__":
    unittest.main()
