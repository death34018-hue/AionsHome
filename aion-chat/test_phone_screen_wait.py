import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import phone_screen


class PhoneScreenWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.phone_dir = self.root / "phone"
        self.uploads_dir = self.root / "uploads"
        self.phone_dir.mkdir()
        self.uploads_dir.mkdir()
        self.meta_path = self.phone_dir / "latest.json"
        self.patchers = [
            patch.object(phone_screen, "PHONE_SCREEN_DIR", self.phone_dir),
            patch.object(phone_screen, "PHONE_SCREEN_META", self.meta_path),
            patch.object(phone_screen, "UPLOADS_DIR", self.uploads_dir),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _write_meta(
        self,
        *,
        received_at: float,
        filename: str = "phone_screen_latest.jpg",
        skip_reason: str = "",
        payload: bytes = b"fresh-phone-screen",
    ) -> Path | None:
        path = None
        if filename:
            path = self.phone_dir / filename
            path.write_bytes(payload)
        self.meta_path.write_text(
            json.dumps({
                "timestamp": received_at - 10,
                "received_at": received_at,
                "filename": filename,
                "path": str(path or ""),
                "skip_reason": skip_reason,
            }),
            encoding="utf-8",
        )
        return path

    def test_result_rejects_stale_receipt_and_accepts_new_receipt(self):
        self._write_meta(received_at=99.0)
        self.assertEqual(
            ("pending", None),
            phone_screen.get_phone_screen_result_after(100.0),
        )

        expected = self._write_meta(received_at=100.1)
        self.assertEqual(
            ("ready", expected),
            phone_screen.get_phone_screen_result_after(100.0),
        )

    async def test_async_wait_returns_when_delayed_upload_arrives(self):
        requested_at = time.time()

        async def upload_later():
            await asyncio.sleep(0.02)
            self._write_meta(received_at=requested_at + 0.01)

        task = asyncio.create_task(upload_later())
        result = await phone_screen.wait_for_phone_screen_after(
            requested_at,
            timeout_seconds=0.2,
            poll_seconds=0.005,
        )
        await task
        self.assertIsNotNone(result)
        self.assertEqual(b"fresh-phone-screen", result.read_bytes())

    async def test_async_wait_stops_immediately_for_new_skip_result(self):
        requested_at = time.time()
        self._write_meta(
            received_at=requested_at + 0.01,
            filename="",
            skip_reason="locked",
        )
        started = time.monotonic()
        result = await phone_screen.wait_for_phone_screen_after(
            requested_at,
            timeout_seconds=1.0,
            poll_seconds=0.01,
        )
        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_sync_wait_times_out_without_a_new_result(self):
        self._write_meta(received_at=50.0)
        result = phone_screen.wait_for_phone_screen_after_sync(
            100.0,
            timeout_seconds=0.02,
            poll_seconds=0.005,
        )
        self.assertIsNone(result)

    def test_freeze_phone_screen_copies_stable_event_attachment(self):
        source = self._write_meta(
            received_at=100.1,
            payload=b"checkpoint-image",
        )
        attachment = phone_screen.freeze_phone_screen(
            source,
            event_id="checkpoint:xhs/20",
        )
        self.assertTrue(attachment.startswith("/uploads/app_supervision_"))
        frozen = self.uploads_dir / attachment.removeprefix("/uploads/")
        self.assertEqual(b"checkpoint-image", frozen.read_bytes())
        self.assertNotIn(":", frozen.name)
        self.assertNotIn("/", frozen.name)


if __name__ == "__main__":
    unittest.main()
