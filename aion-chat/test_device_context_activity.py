import unittest
from datetime import datetime
from unittest.mock import patch

import activity
from device_context import DeviceContextStore


class DeviceContextActivityIntegrationTest(unittest.TestCase):
    def setUp(self):
        store_patch = patch.object(activity, "device_context_store", DeviceContextStore())
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def test_sensor_and_notification_events_do_not_take_app_duration(self):
        start = datetime(2026, 8, 30, 13, 0).timestamp()
        entries = [
            {"device": "phone", "app": "小红书", "timestamp": start},
            {"device": "phone", "app": "device_context", "kind": "phone_context",
             "slot": "motion", "timestamp": start + 30},
            {"device": "phone", "app": "device_context", "kind": "notification",
             "timestamp": start + 90},
            {"device": "phone", "app": "screen_off", "timestamp": start + 720},
        ]
        with (
            patch.object(activity, "read_recent_activity", return_value=entries),
            patch.object(activity.time, "time", return_value=start + 1200),
        ):
            summaries = activity.generate_activity_summary()

        self.assertEqual("手机: 小红书 10分钟", summaries[0]["summary"])
        self.assertEqual(1, summaries[0]["count"])
        self.assertEqual("手机: 锁屏 8分钟, 小红书 2分钟", summaries[1]["summary"])

    def test_monitor_keeps_history_and_lock_transitions_beside_current_state(self):
        start = datetime(2026, 8, 30, 13, 0).timestamp()
        now = start + 300
        events = [(-600, "QQ"), (60, "小红书"), (90, "小红书"),
                  (120, "screen_off"), (140, "screen_on"),
                  (180, "小红书"), (240, "screen_off")]
        entries = [{"device": "phone", "app": app, "timestamp": start + offset}
                   for offset, app in events]
        entries.append({"device": "phone", "app": "device_context",
                        "kind": "phone_context", "slot": "motion", "timestamp": now - 1})
        activity.device_context_store.update_phone({
            "screen": {"value": "off", "observed_at": now, "since": start + 240},
            "foreground_app": {"value": "com.xingin.xhs", "observed_at": start + 180},
        }, now)
        with (
            patch.object(activity, "read_recent_activity", return_value=entries),
            patch.object(activity, "is_activity_tracking_enabled", return_value=True),
            patch.object(activity, "get_current_pc_context", return_value={}),
            patch.object(activity.time, "time", return_value=now),
        ):
            text = activity.get_monitor_activity_for_prompt()

        self.assertIn("【设备当前状态】", text)
        self.assertIn("[12:50~13:00] 手机: QQ 10分钟", text)
        self.assertIn("[13:01:00] 手机：打开了小红书", text)
        self.assertNotIn("13:01:30", text)  # 重复轮询不应挤掉真实切换
        self.assertIn("[13:02:00] 手机：锁屏/熄屏", text)
        self.assertIn("[13:02:20] 手机：亮屏", text)
        self.assertIn("[13:03:00] 手机：打开了小红书", text)
        self.assertIn("[13:04:00] 手机：锁屏/熄屏", text)
        self.assertNotIn("device_context", text)

        with patch.object(activity, "is_activity_tracking_enabled", return_value=False):
            self.assertEqual("", activity.get_monitor_activity_for_prompt())

    def test_activity_logs_keep_eight_hours(self):
        self.assertEqual(8, activity.KEEP_HOURS)

    def test_idle_pc_window_does_not_gain_active_duration(self):
        entries = [
            {
                "device": "pc",
                "app": "Code.exe",
                "title": "AionsHome - Visual Studio Code",
                "timestamp": 100.0,
                "idle_seconds": 601,
                "display": "on",
            }
        ]

        text = activity._summarize_window(entries, 100.0, 700.0, {})

        self.assertNotIn("10分钟", text)
        self.assertNotIn("VS Code", text)

    def test_current_pc_context_includes_specific_window_content(self):
        title = "流浪地球 - Google Chrome"
        entries = [
            {
                "device": "pc",
                "app": "chrome.exe",
                "title": title,
                "timestamp": 100.0,
            }
        ]

        with (
            patch.object(activity, "read_recent_activity", return_value=entries),
            patch.object(
                activity.pc_display_tracker,
                "get_status",
                return_value={"physical_state": "on", "idle_seconds": 3},
            ),
        ):
            context = activity.get_current_pc_context(now=101.0)

        self.assertEqual("Chrome（流浪地球）", context["app"])
        self.assertEqual(title, context["title"])

    def test_phone_ingest_persists_only_stable_changes_and_builds_status(self):
        payload = {
            "screen": {"value": "on", "observed_at": 100.0},
            "posture": {"value": "portrait", "observed_at": 100.0},
            "motion": {"value": "slight", "observed_at": 100.0},
        }
        with patch.object(activity, "append_activity_log") as append:
            activity.record_phone_context(payload, received_at=100.0)
            activity.record_phone_context(payload, received_at=110.0)

        snapshot = activity.get_device_context_snapshot(
            now=110.0,
            pc={"display": "off", "idle_seconds": 999},
        )

        self.assertEqual("portrait", snapshot["phone"]["posture"]["value"])
        self.assertEqual(3, append.call_count)


if __name__ == "__main__":
    unittest.main()
