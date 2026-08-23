import unittest
from unittest.mock import patch

import activity
from device_context import DeviceContextStore


class DeviceContextActivityIntegrationTest(unittest.TestCase):
    def setUp(self):
        activity.device_context_store = DeviceContextStore()

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
