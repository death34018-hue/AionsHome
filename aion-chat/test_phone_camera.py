import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from phone_camera import PhoneCameraCoordinator


def _jpeg(width: int = 64, height: int = 48) -> bytes:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = 180
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise AssertionError("failed to build JPEG fixture")
    return encoded.tobytes()


async def _wait_for_count(items: list, count: int, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while len(items) < count:
        if time.monotonic() >= deadline:
            raise AssertionError(f"expected {count} items, got {len(items)}")
        await asyncio.sleep(0.002)


class PhoneCameraCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.coordinator = PhoneCameraCoordinator(
            capture_dir=root / "captures",
            upload_dir=root / "uploads",
        )
        self.coordinator.arm(
            client_id="android-push:test-device",
            facing="front",
            zoom=1.0,
            capabilities={
                "front": {"minZoom": 1.0, "maxZoom": 4.0},
                "back": {"minZoom": 0.6, "maxZoom": 10.0},
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_upload_only_completes_matching_request(self):
        sent = []

        async def send_command(client_id, event):
            sent.append((client_id, event))
            return True

        task = asyncio.create_task(
            self.coordinator.request_capture(
                "ai_cam_check",
                send_command,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(sent, 1)
        request_id = sent[0][1]["data"]["request_id"]

        unknown = self.coordinator.accept_upload(
            "different-request",
            _jpeg(),
            {"facing": "front", "zoom": 1.0},
        )
        self.assertEqual("unknown_request", unknown.status)
        self.assertFalse(task.done())

        accepted = self.coordinator.accept_upload(
            request_id,
            _jpeg(),
            {"facing": "front", "zoom": 1.0},
        )
        result = await task

        self.assertEqual("ready", accepted.status)
        self.assertEqual("ready", result.status)
        self.assertEqual(request_id, result.request_id)
        self.assertEqual(64, result.width)
        self.assertEqual(48, result.height)
        self.assertTrue(result.path.is_file())

    async def test_duplicate_upload_is_idempotent(self):
        sent = []

        async def send_command(client_id, event):
            sent.append(event)
            return True

        task = asyncio.create_task(
            self.coordinator.request_capture(
                "scheduled_monitor",
                send_command,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(sent, 1)
        request_id = sent[0]["data"]["request_id"]
        first = self.coordinator.accept_upload(request_id, _jpeg(), {})
        second = self.coordinator.accept_upload(request_id, _jpeg(), {})
        result = await task

        self.assertEqual("ready", first.status)
        self.assertEqual("duplicate", second.status)
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.path, result.path)

    async def test_late_upload_cannot_complete_newer_request(self):
        first_sent = []

        async def send_first(client_id, event):
            first_sent.append(event)
            return True

        first_result = await self.coordinator.request_capture(
            "sentinel_patrol",
            send_first,
            timeout_seconds=0.03,
            retry_after_seconds=0.01,
        )
        first_id = first_sent[0]["data"]["request_id"]
        self.assertEqual("timeout", first_result.status)

        second_sent = []

        async def send_second(client_id, event):
            second_sent.append(event)
            return True

        second_task = asyncio.create_task(
            self.coordinator.request_capture(
                "sentinel_patrol",
                send_second,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(second_sent, 1)
        second_id = second_sent[0]["data"]["request_id"]

        late = self.coordinator.accept_upload(first_id, _jpeg(), {})
        self.assertEqual("expired", late.status)
        self.assertFalse(second_task.done())

        self.coordinator.accept_upload(second_id, _jpeg(), {})
        second_result = await second_task
        self.assertEqual("ready", second_result.status)
        self.assertEqual(second_id, second_result.request_id)

    async def test_invalid_and_oversized_jpeg_are_rejected(self):
        invalid_sent = []

        async def send_invalid(client_id, event):
            invalid_sent.append(event)
            return True

        invalid_task = asyncio.create_task(
            self.coordinator.request_capture(
                "ai_cam_check",
                send_invalid,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(invalid_sent, 1)
        invalid_id = invalid_sent[0]["data"]["request_id"]
        invalid = self.coordinator.accept_upload(invalid_id, b"not-a-jpeg", {})
        self.assertEqual("invalid_image", invalid.status)
        self.assertFalse(invalid_task.done())
        invalid_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invalid_task

        root = Path(self.tmp.name)
        limited = PhoneCameraCoordinator(
            capture_dir=root / "limited-captures",
            upload_dir=root / "limited-uploads",
            max_upload_bytes=100,
        )
        limited.arm("android-push:test-device", "back", 1.0, {})
        oversized_sent = []

        async def send_oversized(client_id, event):
            oversized_sent.append(event)
            return True

        oversized_task = asyncio.create_task(
            limited.request_capture(
                "scheduled_monitor",
                send_oversized,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(oversized_sent, 1)
        oversized_id = oversized_sent[0]["data"]["request_id"]
        oversized = limited.accept_upload(oversized_id, _jpeg(), {})
        self.assertEqual("too_large", oversized.status)
        oversized_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await oversized_task

    async def test_async_request_retries_same_id_then_times_out(self):
        sent = []

        async def send_command(client_id, event):
            sent.append(event)
            return True

        result = await self.coordinator.request_capture(
            "scheduled_monitor",
            send_command,
            timeout_seconds=0.06,
            retry_after_seconds=0.02,
        )

        self.assertEqual("timeout", result.status)
        self.assertEqual(2, len(sent))
        self.assertEqual(
            sent[0]["data"]["request_id"],
            sent[1]["data"]["request_id"],
        )

    async def test_phone_failure_completes_wait_without_image(self):
        sent = []

        async def send_command(client_id, event):
            sent.append(event)
            return True

        task = asyncio.create_task(
            self.coordinator.request_capture(
                "ai_cam_check",
                send_command,
                timeout_seconds=0.3,
                retry_after_seconds=0.2,
            )
        )
        await _wait_for_count(sent, 1)
        request_id = sent[0]["data"]["request_id"]

        accepted = self.coordinator.accept_failure(
            request_id,
            "camera_permission_denied",
            {"facing": "front"},
        )
        result = await task

        self.assertEqual("failed", accepted.status)
        self.assertEqual("failed", result.status)
        self.assertEqual("camera_permission_denied", result.error)
        self.assertEqual({"facing": "front"}, result.metadata)

    async def test_only_allowed_reasons_can_create_requests(self):
        async def send_command(client_id, event):
            return True

        with self.assertRaisesRegex(ValueError, "unsupported capture reason"):
            await self.coordinator.request_capture(
                "manual_preview",
                send_command,
                timeout_seconds=0.01,
                retry_after_seconds=0.005,
            )

    def test_sync_request_honors_should_continue(self):
        sent = []
        continue_flag = threading.Event()

        def send_command(client_id, event):
            sent.append(event)
            return True

        result = self.coordinator.request_capture_sync(
            "sentinel_patrol",
            send_command,
            timeout_seconds=1.0,
            retry_after_seconds=0.5,
            should_continue=continue_flag.is_set,
        )

        self.assertEqual("cancelled", result.status)
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
