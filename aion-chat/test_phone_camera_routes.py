import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import camera
from phone_camera import PhoneCameraCoordinator


def _jpeg() -> bytes:
    image = np.full((48, 64, 3), 90, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise AssertionError("fixture JPEG encoding failed")
    return encoded.tobytes()


class PhoneCameraRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordinator = PhoneCameraCoordinator(
            capture_dir=root / "captures",
            upload_dir=root / "uploads",
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_arm_requires_android_client_id_and_valid_facing(self):
        from routes import phone_camera as routes

        with patch.object(routes, "phone_camera", self.coordinator):
            with self.assertRaisesRegex(ValueError, "Android push"):
                await routes.arm_phone_camera(
                    routes.PhoneCameraArm(
                        client_id="browser:123",
                        facing="front",
                        zoom=1.0,
                        capabilities={},
                    )
                )
            with self.assertRaisesRegex(ValueError, "front or back"):
                await routes.arm_phone_camera(
                    routes.PhoneCameraArm(
                        client_id="android-push:device",
                        facing="side",
                        zoom=1.0,
                        capabilities={},
                    )
                )

    async def test_upload_reports_unknown_request_without_overwriting_state(self):
        from routes import phone_camera as routes

        with patch.object(routes, "phone_camera", self.coordinator):
            result = routes.accept_phone_camera_upload(
                "unknown",
                _jpeg(),
                {"facing": "front"},
            )
            status = await routes.phone_camera_status()

        self.assertEqual("unknown_request", result["status"])
        self.assertEqual("never", status["last_capture"]["status"])

    async def test_phone_source_status_exposes_armed_and_last_capture_metadata(self):
        from routes import phone_camera as routes

        with patch.object(routes, "phone_camera", self.coordinator):
            await routes.arm_phone_camera(
                routes.PhoneCameraArm(
                    client_id="android-push:device",
                    facing="back",
                    zoom=0.6,
                    capabilities={
                        "back": {"minZoom": 0.6, "maxZoom": 10.0}
                    },
                )
            )
            status = await routes.phone_camera_status()

        self.assertTrue(status["armed"])
        self.assertEqual("back", status["facing"])
        self.assertEqual(0.6, status["zoom"])

    async def test_preview_upload_never_completes_capture_request(self):
        from routes import phone_camera as routes

        self.coordinator.arm("android-push:device", "front", 1.0, {})
        sent = []

        async def send_command(client_id, event):
            sent.append(event)
            return True

        with patch.object(routes, "phone_camera", self.coordinator):
            request_task = asyncio.create_task(
                self.coordinator.request_capture(
                    "ai_cam_check",
                    send_command,
                    timeout_seconds=0.2,
                    retry_after_seconds=0.1,
                )
            )
            while not sent:
                await asyncio.sleep(0.002)
            preview = routes.accept_phone_camera_preview(
                _jpeg(),
                {"facing": "front", "zoom": 1.0},
            )
            self.assertTrue(preview["ok"])
            self.assertFalse(request_task.done())
            request_id = sent[0]["data"]["request_id"]
            self.coordinator.accept_upload(request_id, _jpeg(), {})
            result = await request_task

        self.assertEqual("ready", result.status)

    def test_switch_source_accepts_phone_without_opening_usb_camera(self):
        monitor = camera.CameraMonitor()
        monitor.cfg = {
            "camera_index": 0,
            "active_source": "local",
            "esp32_cam_url": "",
        }

        with (
            patch.object(monitor, "_stop_current_source") as stop_source,
            patch.object(monitor, "open_camera") as open_camera,
            patch.object(monitor, "open_esp32") as open_esp32,
            patch("camera.save_cam_config"),
        ):
            result = monitor.switch_source("phone")

        self.assertTrue(result)
        self.assertEqual("phone", monitor.cfg["active_source"])
        stop_source.assert_called()
        open_camera.assert_not_called()
        open_esp32.assert_not_called()


if __name__ == "__main__":
    unittest.main()
