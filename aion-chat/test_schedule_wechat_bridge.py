import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ScheduleWeChatBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_chatroom_reply_creates_monitor_and_hides_command(self):
        import schedule

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        db_path = Path(tempdir.name) / "schedule.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE schedules (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    trigger_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    origin TEXT DEFAULT 'aion',
                    origin_room_id TEXT DEFAULT '',
                    ended_at REAL
                )
                """
            )
            await db.commit()

        def connection():
            return aiosqlite.connect(db_path)

        passthrough = AsyncMock(side_effect=lambda text, *args, **kwargs: text)
        no_supervision = AsyncMock(side_effect=lambda text, **kwargs: (text, None))
        with patch.object(schedule, "get_db", connection), \
             patch("routes.chat._process_home_commands", passthrough), \
             patch.object(schedule, "_process_background_wechat_commands", passthrough), \
             patch.object(schedule, "process_band_vibration", passthrough), \
             patch.object(schedule, "process_hug_pillow_commands", passthrough), \
             patch("app_supervision_ai.queue_app_supervision_reply_command", no_supervision), \
             patch("app_supervision_ai.broadcast_app_supervision_command", AsyncMock()):
            cleaned = await schedule._process_background_reply_commands(
                "先吃正餐。[Monitor:2030-08-07T12:35|确认已经吃午饭]",
                target={"type": "chatroom", "room_id": "room-1"},
                conv_id=None,
                sender="connor",
                ai_msg_id="msg-1",
            )

        async with aiosqlite.connect(db_path) as db:
            row = await (
                await db.execute(
                    "SELECT type, trigger_at, content, origin, origin_room_id FROM schedules"
                )
            ).fetchone()

        self.assertEqual(cleaned, "先吃正餐。")
        self.assertEqual(
            row,
            ("monitor", "2030-08-07 12:35", "确认已经吃午饭", "connor", "room-1"),
        )

    async def test_background_chatroom_reply_processes_wechat_command(self):
        import schedule

        save_chatroom_system = AsyncMock()
        send_wechat_message = AsyncMock()
        record_route = AsyncMock()

        with patch.object(schedule, "_chatroom_sys_msg", save_chatroom_system), \
             patch.object(schedule, "dispatch_wechat_message", send_wechat_message), \
             patch.object(schedule, "record_wechat_route", record_route), \
             patch("wechat_bridge.resolve_wechat_sender_label", return_value="Companion"):
            cleaned = await schedule._process_background_wechat_commands(
                "本地提醒。[微信消息：去看手机亮没亮]",
                target={"type": "chatroom", "room_id": "room-1"},
                conv_id="conv-1",
                sender="connor",
                ai_msg_id="msg-1",
            )

        self.assertEqual(cleaned, "本地提醒。")
        save_chatroom_system.assert_awaited_once_with(
            "room-1",
            "本条为微信消息：Companion：去看手机亮没亮",
            after_msg_id="msg-1",
        )
        record_route.assert_awaited_once_with({
            "source_type": "chatroom",
            "source_id": "room-1",
            "sender": "connor",
            "source_msg_id": "msg-1",
        })
        send_wechat_message.assert_awaited_once_with(
            content="Companion：去看手机亮没亮",
            source_type="chatroom",
            source_id="room-1",
            sender="connor",
            source_msg_id="msg-1",
        )

    async def test_background_private_reply_processes_wechat_command(self):
        import schedule

        save_private_system = AsyncMock()
        send_wechat_message = AsyncMock()

        with patch.object(schedule, "_sys_msg", save_private_system), \
             patch.object(schedule, "dispatch_wechat_message", send_wechat_message):
            cleaned = await schedule._process_background_wechat_commands(
                "本地提醒。[微信消息：去看手机亮没亮]",
                target={"type": "private"},
                conv_id="conv-1",
                sender="aion",
                ai_msg_id="msg-1",
            )

        self.assertEqual(cleaned, "本地提醒。")
        save_private_system.assert_awaited_once_with(
            "conv-1",
            "本条为微信消息：去看手机亮没亮",
            after_msg_id="msg-1",
        )
        send_wechat_message.assert_awaited_once_with(
            content="去看手机亮没亮",
            source_type="aion_private",
            source_id="conv-1",
            sender="aion",
            source_msg_id="msg-1",
        )

    async def test_background_reply_processes_home_before_wechat(self):
        import schedule

        home_processor = AsyncMock(return_value="本地提醒。[微信消息：去看手机亮没亮]")
        wechat_processor = AsyncMock(return_value="本地提醒。")

        with patch("routes.chat._process_home_commands", home_processor), \
             patch.object(schedule, "_process_background_wechat_commands", wechat_processor):
            cleaned = await schedule._process_background_reply_commands(
                "本地提醒。[HOME:on|客厅灯][微信消息：去看手机亮没亮]",
                target={"type": "private"},
                conv_id="conv-1",
                sender="aion",
                ai_msg_id="msg-1",
            )

        self.assertEqual(cleaned, "本地提醒。")
        home_processor.assert_awaited_once_with("本地提醒。[HOME:on|客厅灯][微信消息：去看手机亮没亮]")
        wechat_processor.assert_awaited_once_with(
            "本地提醒。[微信消息：去看手机亮没亮]",
            target={"type": "private"},
            conv_id="conv-1",
            sender="aion",
            ai_msg_id="msg-1",
        )


class ScheduleWeChatWiringTests(unittest.TestCase):
    def test_alarm_and_monitor_paths_run_background_reply_postprocessing_before_save(self):
        source = (ROOT / "schedule.py").read_text(encoding="utf-8")
        alarm_start = source.index("    async def _fire_alarm")
        monitor_start = source.index("    async def _fire_monitor")
        parser_start = source.index("async def process_schedule_commands")
        alarm_block = source[alarm_start:monitor_start]
        monitor_block = source[monitor_start:parser_start]

        self.assertIn("_process_background_reply_commands", alarm_block)
        self.assertIn("_process_background_reply_commands", monitor_block)

    def test_triggered_reply_paths_run_background_reply_postprocessing(self):
        schedule_source = (ROOT / "schedule.py").read_text(encoding="utf-8")
        camera_source = (ROOT / "camera.py").read_text(encoding="utf-8")
        location_source = (ROOT / "location.py").read_text(encoding="utf-8")
        chat_source = (ROOT / "routes" / "chat.py").read_text(encoding="utf-8")
        chatroom_source = (ROOT / "routes" / "chatroom.py").read_text(encoding="utf-8")

        self.assertIn("async def _process_background_reply_commands", schedule_source)
        self.assertIn("_process_background_reply_commands", camera_source[camera_source.index("async def _call_core"):camera_source.index("cam = CameraMonitor()")])
        self.assertIn("_process_background_reply_commands", camera_source[camera_source.index("async def perform_cam_check"):])
        self.assertIn("_process_background_reply_commands", location_source[location_source.index("async def _call_core_location"):])
        self.assertIn("_process_background_reply_commands", chat_source[chat_source.index("async def perform_poi_check"):chat_source.index("async def perform_activity_check")])
        self.assertIn("_process_background_reply_commands", chat_source[chat_source.index("async def perform_activity_check"):])
        self.assertGreaterEqual(chatroom_source.count("_process_background_reply_commands"), 3)


if __name__ == "__main__":
    unittest.main()
