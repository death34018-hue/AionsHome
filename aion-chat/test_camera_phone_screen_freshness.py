import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import camera


class CameraPhoneScreenFreshnessTests(unittest.TestCase):
    def test_overlay_forwards_request_receipt_lower_bound(self):
        monitor = camera.CameraMonitor()
        screen = np.zeros((12, 12, 3), dtype=np.uint8)
        with patch(
            "phone_screen.get_recent_phone_screen_path",
            return_value=None,
        ) as recent:
            result = monitor._overlay_phone_screen(
                screen,
                phone_screen_after=123.5,
            )
        self.assertIs(result, screen)
        recent.assert_called_once_with(
            max_age_seconds=150,
            received_after=123.5,
        )

    def test_frame_capture_propagates_request_receipt_lower_bound(self):
        monitor = camera.CameraMonitor()
        monitor._latest_frame = np.zeros((12, 12, 3), dtype=np.uint8)
        combined = np.zeros((12, 12, 3), dtype=np.uint8)
        with patch.object(monitor, "_combine_with_screen", return_value=combined) as combine:
            self.assertIsNotNone(
                monitor.get_frame_jpeg(phone_screen_after=456.5)
            )
        combine.assert_called_once()
        self.assertEqual(456.5, combine.call_args.kwargs["phone_screen_after"])

    def test_patrol_and_scheduled_monitor_use_condition_waiting(self):
        camera_source = Path(camera.__file__).read_text(encoding="utf-8")
        patrol_start = camera_source.index("    def _monitor_loop(self):")
        patrol_end = camera_source.index("    async def _analyze_and_log", patrol_start)
        patrol = camera_source[patrol_start:patrol_end]
        self.assertIn("wait_for_phone_screen_after_sync", patrol)
        self.assertNotIn("time.sleep(5)", patrol)

        schedule_path = Path(camera.__file__).with_name("schedule.py")
        schedule_source = schedule_path.read_text(encoding="utf-8")
        monitor_start = schedule_source.index("    async def _fire_monitor")
        monitor_end = schedule_source.index("async def process_schedule_commands", monitor_start)
        scheduled_monitor = schedule_source[monitor_start:monitor_end]
        self.assertIn("wait_for_phone_screen_after", scheduled_monitor)
        self.assertNotIn("await asyncio.sleep(5)", scheduled_monitor)


if __name__ == "__main__":
    unittest.main()
