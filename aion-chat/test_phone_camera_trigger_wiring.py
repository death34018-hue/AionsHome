import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np

import camera
from phone_camera import PhoneCameraCoordinator


def _jpeg() -> bytes:
    frame = np.full((60, 80, 3), 140, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise AssertionError("JPEG fixture failed")
    return encoded.tobytes()


class PhoneCameraTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordinator = PhoneCameraCoordinator(
            capture_dir=root / "captures",
            upload_dir=root / "uploads",
        )
        self.coordinator.arm("android-push:device", "front", 1.0, {})

    def tearDown(self):
        self.tmp.cleanup()

    async def _capture_for_reason(self, reason):
        async def send(client_id, event):
            request_id = event["data"]["request_id"]
            self.coordinator.accept_upload(request_id, _jpeg(), {})
            return True

        with (
            patch.object(camera, "phone_camera", self.coordinator),
            patch.object(camera.manager, "send_to_client", side_effect=send),
            patch("phone_screen.wait_for_phone_screen_after",
                  new_callable=AsyncMock, return_value=None),
            patch.object(camera.cam, "_combine_with_screen",
                         side_effect=lambda frame, **kwargs: frame),
        ):
            camera.cam.cfg["active_source"] = "phone"
            return await camera.acquire_monitor_image(
                reason,
                timeout_seconds=0.1,
                retry_after_seconds=0.05,
            )

    async def test_scheduled_monitor_uses_request_correlated_phone_image(self):
        result = await self._capture_for_reason("scheduled_monitor")
        self.assertEqual("phone", result.source)
        self.assertEqual("ready", result.status)
        decoded = cv2.imdecode(
            np.frombuffer(result.jpeg, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        self.assertEqual((60, 80), decoded.shape[:2])

    async def test_private_and_chatroom_ai_checks_share_ai_reason(self):
        first = await self._capture_for_reason("ai_cam_check")
        second = await self._capture_for_reason("ai_cam_check")
        self.assertEqual("ready", first.status)
        self.assertEqual("ready", second.status)
        self.assertNotEqual(first.request_id, second.request_id)

    async def test_phone_failure_has_no_attachment_and_forbids_inference(self):
        async def send(client_id, event):
            return True

        with (
            patch.object(camera, "phone_camera", self.coordinator),
            patch.object(camera.manager, "send_to_client", side_effect=send),
        ):
            camera.cam.cfg["active_source"] = "phone"
            result = await camera.acquire_monitor_image(
                "ai_cam_check",
                timeout_seconds=0.02,
                retry_after_seconds=0.01,
            )

        self.assertEqual("timeout", result.status)
        self.assertIsNone(result.jpeg)
        self.assertIn("没有图像附件", result.no_image_context)
        self.assertIn("不得描述或推断", result.no_image_context)

    async def test_local_source_does_not_send_phone_command(self):
        frame = np.full((48, 64, 3), 200, dtype=np.uint8)
        with camera.cam._lock:
            camera.cam._latest_frame = frame
        camera.cam.cfg["active_source"] = "local"

        with patch.object(
            camera.manager,
            "send_to_client",
            new_callable=AsyncMock,
        ) as send:
            result = await camera.acquire_monitor_image("ai_cam_check")

        self.assertEqual("camera", result.source)
        self.assertEqual("ready", result.status)
        self.assertTrue(result.jpeg)
        send.assert_not_awaited()


class PhoneCameraSyncTriggerTests(unittest.TestCase):
    def test_sentinel_patrol_uses_sync_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordinator = PhoneCameraCoordinator(
                capture_dir=root / "captures",
                upload_dir=root / "uploads",
            )
            coordinator.arm("android-push:device", "back", 1.0, {})

            def send(client_id, event):
                coordinator.accept_upload(
                    event["data"]["request_id"],
                    _jpeg(),
                    {},
                )
                return True

            with patch.object(camera, "phone_camera", coordinator):
                with (
                    patch("phone_screen.wait_for_phone_screen_after_sync",
                          return_value=None),
                    patch.object(camera.cam, "_combine_with_screen",
                                 side_effect=lambda frame, **kwargs: frame),
                ):
                    camera.cam.cfg["active_source"] = "phone"
                    result = camera.acquire_monitor_image_sync(
                        "sentinel_patrol",
                        send_command=send,
                        timeout_seconds=0.1,
                        retry_after_seconds=0.05,
                    )

        self.assertEqual("ready", result.status)
        self.assertEqual("phone", result.source)
        self.assertTrue(result.jpeg)

    def test_phone_camera_is_composed_with_existing_device_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordinator = PhoneCameraCoordinator(
                capture_dir=root / "captures",
                upload_dir=root / "uploads",
            )
            coordinator.arm("android-push:device", "back", 1.0, {})

            def send(client_id, event):
                coordinator.accept_upload(
                    event["data"]["request_id"],
                    _jpeg(),
                    {},
                )
                return True

            def add_device_layer(frame, **kwargs):
                device = np.full_like(frame, 35)
                return np.vstack([frame, device])

            with (
                patch.object(camera, "phone_camera", coordinator),
                patch("phone_screen.wait_for_phone_screen_after_sync",
                      return_value=None),
                patch.object(camera.cam, "_combine_with_screen",
                             side_effect=add_device_layer) as combine,
            ):
                camera.cam.cfg["active_source"] = "phone"
                result = camera.acquire_monitor_image_sync(
                    "sentinel_patrol",
                    send_command=send,
                    timeout_seconds=0.1,
                    retry_after_seconds=0.05,
                )

        decoded = cv2.imdecode(
            np.frombuffer(result.jpeg, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        self.assertEqual((120, 80), decoded.shape[:2])
        self.assertIsNotNone(
            combine.call_args.kwargs["phone_screen_after"]
        )

    def test_failed_sentinel_capture_still_notifies_core_without_attachment(self):
        monitor = camera.CameraMonitor()
        result = camera.MonitorImageResult(
            status="timeout",
            source="phone",
            request_id="cam_timeout",
            no_image_context=camera.PHONE_CAMERA_NO_IMAGE_CONTEXT,
            error="capture_timeout",
        )

        async def exercise():
            with (
                patch("camera.async_get_last_aion_timeline_user_msg_time",
                      new_callable=AsyncMock, return_value=123.0),
                patch("camera.read_logs_since", return_value=[]),
                patch("camera.append_monitor_log"),
                patch.object(camera.manager, "broadcast", new_callable=AsyncMock),
                patch.object(
                    monitor,
                    "_wake_core_targets",
                    new_callable=AsyncMock,
                ) as wake_targets,
                patch.object(monitor, "_call_core", new_callable=AsyncMock),
            ):
                await monitor._handle_monitor_no_image(result)
                return wake_targets

        wake_targets = asyncio.run(exercise())
        self.assertEqual(1, wake_targets.await_count)
        args = wake_targets.await_args.args
        self.assertEqual(["aion"], args[0])
        self.assertIn("没有图像附件", args[1])
        self.assertEqual("", args[6])


if __name__ == "__main__":
    unittest.main()
