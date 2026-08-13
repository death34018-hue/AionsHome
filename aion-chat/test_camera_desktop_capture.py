import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import camera


class CameraDesktopCaptureCompositionTests(unittest.TestCase):
    def _write_phone_screen(self, directory: str, frame: np.ndarray) -> Path:
        path = Path(directory) / "phone-screen.png"
        self.assertTrue(cv2.imwrite(str(path), frame))
        return path

    def test_disabled_desktop_capture_places_portrait_phone_left_of_camera(self):
        monitor = camera.CameraMonitor()
        monitor.cfg["include_pc_screen"] = False
        camera_frame = np.full((200, 100, 3), (0, 180, 0), dtype=np.uint8)
        phone_frame = np.full((400, 200, 3), (180, 0, 0), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as tmpdir:
            phone_path = self._write_phone_screen(tmpdir, phone_frame)
            with (
                patch.object(
                    monitor,
                    "_capture_screen",
                    side_effect=AssertionError("desktop capture must stay disabled"),
                ),
                patch(
                    "phone_screen.get_recent_phone_screen_path",
                    return_value=phone_path,
                ),
            ):
                result = monitor._combine_with_screen(
                    camera_frame,
                    force_pc_screen=True,
                    phone_screen_after=123.5,
                )

        self.assertEqual((200, 200), result.shape[:2])
        self.assertGreater(float(result[80:, :100, 0].mean()), 160)
        self.assertLess(float(result[80:, :100, 1].mean()), 20)
        self.assertGreater(float(result[80:, 100:, 1].mean()), 160)
        self.assertLess(float(result[80:, 100:, 0].mean()), 20)

    def test_disabled_desktop_capture_without_phone_returns_camera_only(self):
        monitor = camera.CameraMonitor()
        monitor.cfg["include_pc_screen"] = False
        camera_frame = np.full((120, 160, 3), (0, 180, 0), dtype=np.uint8)

        with (
            patch.object(
                monitor,
                "_capture_screen",
                side_effect=AssertionError("desktop capture must stay disabled"),
            ),
            patch(
                "phone_screen.get_recent_phone_screen_path",
                return_value=None,
            ),
        ):
            result = monitor._combine_with_screen(camera_frame, force_pc_screen=True)

        self.assertEqual((120, 160), result.shape[:2])
        self.assertGreater(float(result[50:, :, 1].mean()), 160)

    def test_disabled_screen_only_capture_returns_phone_without_pc_capture(self):
        monitor = camera.CameraMonitor()
        monitor.cfg["include_pc_screen"] = False
        phone_frame = np.full((180, 90, 3), (180, 0, 0), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as tmpdir:
            phone_path = self._write_phone_screen(tmpdir, phone_frame)
            with (
                patch.object(
                    monitor,
                    "_capture_screen",
                    side_effect=AssertionError("desktop capture must stay disabled"),
                ),
                patch(
                    "phone_screen.get_recent_phone_screen_path",
                    return_value=phone_path,
                ),
            ):
                encoded = monitor.get_screen_only_jpeg(force_pc_screen=True)

        result = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual((180, 90), result.shape[:2])
        self.assertGreater(float(result[:, :, 0].mean()), 160)

    def test_enabled_desktop_capture_keeps_vertical_camera_over_desktop(self):
        monitor = camera.CameraMonitor()
        monitor.cfg["include_pc_screen"] = True
        camera_frame = np.full((100, 200, 3), (0, 180, 0), dtype=np.uint8)
        desktop_frame = np.full((50, 100, 3), (0, 0, 180), dtype=np.uint8)

        with (
            patch.object(monitor, "_capture_screen", return_value=desktop_frame) as capture,
            patch(
                "phone_screen.get_recent_phone_screen_path",
                return_value=None,
            ),
        ):
            result = monitor._combine_with_screen(camera_frame, force_pc_screen=True)

        capture.assert_called_once_with(force=True)
        self.assertEqual((200, 200), result.shape[:2])
        self.assertGreater(float(result[50:100, :, 1].mean()), 160)
        self.assertGreater(float(result[150:, :, 2].mean()), 160)


class CameraMonitorPromptLayoutTests(unittest.TestCase):
    def test_disabled_layout_guidance_describes_phone_left_of_camera_by_label(self):
        guidance = camera.build_monitor_layout_guidance(False)

        self.assertIn("PHONE SCREEN", guidance)
        self.assertIn("CAMERA VIEW", guidance)
        self.assertIn("左侧", guidance)
        self.assertNotIn("下半部分", guidance)
        self.assertNotIn("左下方", guidance)

    def test_enabled_layout_guidance_allows_pc_device_context(self):
        guidance = camera.build_monitor_layout_guidance(True)

        self.assertIn("DEVICE CONTEXT", guidance)
        self.assertIn("电脑桌面", guidance)
        self.assertIn("PHONE SCREEN", guidance)


if __name__ == "__main__":
    unittest.main()
