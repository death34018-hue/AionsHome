import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import cv2
import numpy as np

import camera
import schedule
from routes import chatroom as chatroom_routes


def _jpeg(frame: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise AssertionError("failed to encode JPEG fixture")
    return encoded.tobytes()


class MonitorCameraCaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_local_capture_returns_pure_and_composite_images_from_one_frame(self):
        monitor = camera.CameraMonitor()
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[:, :, 1] = 180
        monitor._latest_frame = frame
        monitor._combine_with_screen = lambda source, **_: np.vstack(
            [source, np.full_like(source, 25)]
        )

        composite_jpeg, camera_jpeg = monitor.get_capture_jpegs()

        pure = cv2.imdecode(
            np.frombuffer(camera_jpeg, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        composite = cv2.imdecode(
            np.frombuffer(composite_jpeg, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        self.assertEqual((48, 64), pure.shape[:2])
        self.assertEqual((96, 64), composite.shape[:2])
        self.assertGreater(float(pure[:, :, 1].mean()), 150)

    def test_phone_capture_reuses_uploaded_camera_jpeg(self):
        frame = np.zeros((40, 60, 3), dtype=np.uint8)
        frame[:, :, 2] = 210
        payload = _jpeg(frame)

        with tempfile.TemporaryDirectory() as tmpdir:
            upload_path = Path(tmpdir) / "phone-camera.jpg"
            upload_path.write_bytes(payload)
            result = SimpleNamespace(
                status="ready",
                path=upload_path,
                request_id="phone-request",
                error="",
            )
            with patch.object(
                camera.cam,
                "_combine_with_screen",
                side_effect=lambda source, **_: source,
            ):
                monitor_result = camera._phone_result_to_monitor(result)

        self.assertEqual(payload, monitor_result.camera_jpeg)
        self.assertIsNotNone(monitor_result.jpeg)

    async def test_screen_only_fallback_never_becomes_camera_snapshot(self):
        with (
            patch.dict(camera.cam.cfg, {"active_source": "local"}),
            patch.object(
                camera.cam,
                "get_capture_jpegs",
                return_value=(None, None),
            ),
            patch.object(
                camera.cam,
                "get_screen_only_jpeg",
                return_value=b"screen-only",
            ),
        ):
            result = await camera.acquire_monitor_image("scheduled_monitor")

        self.assertEqual("device", result.source)
        self.assertEqual(b"screen-only", result.jpeg)
        self.assertIsNone(result.camera_jpeg)

    def test_stable_snapshot_attachment_is_written_only_for_camera_bytes(self):
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        payload = _jpeg(frame)

        with tempfile.TemporaryDirectory() as tmpdir:
            upload_dir = Path(tmpdir)
            attachment = camera.save_monitor_camera_snapshot(
                payload,
                "scheduled",
                upload_dir=upload_dir,
            )
            empty = camera.save_monitor_camera_snapshot(
                None,
                "scheduled",
                upload_dir=upload_dir,
            )

            saved = upload_dir / Path(attachment["url"]).name
            self.assertTrue(saved.is_file())
            self.assertEqual(payload, saved.read_bytes())
            self.assertEqual("monitor_camera_snapshot", attachment["type"])
            self.assertEqual(None, empty)
            self.assertEqual(1, len(list(upload_dir.glob("monitor_camera_*.jpg"))))


class MonitorSystemAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "messages.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attachments TEXT DEFAULT '[]',
                    reasoning_content TEXT DEFAULT ''
                );
                CREATE TABLE chatroom_rooms (
                    id TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attachments TEXT DEFAULT '[]',
                    reasoning_content TEXT DEFAULT ''
                );
                INSERT INTO conversations (id, updated_at) VALUES ('conv-1', 0);
                INSERT INTO chatroom_rooms (id, updated_at) VALUES ('room-1', 0);
                """
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        db_path = self.db_path

        @asynccontextmanager
        async def connection():
            async with aiosqlite.connect(db_path) as db:
                yield db

        return connection()

    async def _attachments_for(self, table: str, role_column: str, role: str):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"SELECT attachments FROM {table} WHERE {role_column}=?",
                (role,),
            )
            row = await cur.fetchone()
        return json.loads(row[0])

    async def test_private_monitor_snapshot_is_saved_on_system_message_only(self):
        snapshot = {
            "type": "monitor_camera_snapshot",
            "url": "/uploads/private.jpg",
        }
        manager = schedule.ScheduleManager()
        with (
            patch.object(schedule, "get_db", side_effect=self._get_db),
            patch.object(schedule.manager, "broadcast", new=AsyncMock()),
            patch.object(
                schedule,
                "with_band_vibration_attachment",
                new=AsyncMock(return_value=[]),
            ),
            patch("routes.files.export_conversation", new=AsyncMock()),
        ):
            await manager._save_to_private(
                "conv-1",
                "Configured AI查看了监控",
                "reply",
                "assistant-private",
                "[]",
                [],
                system_atts=[snapshot],
            )

        self.assertEqual(
            [snapshot],
            await self._attachments_for("messages", "role", "system"),
        )
        self.assertEqual(
            [],
            await self._attachments_for("messages", "role", "assistant"),
        )

    async def test_chatroom_monitor_snapshot_is_saved_on_system_message_only(self):
        snapshot = {
            "type": "monitor_camera_snapshot",
            "url": "/uploads/chatroom.jpg",
        }
        manager = schedule.ScheduleManager()
        with (
            patch.object(schedule, "get_db", side_effect=self._get_db),
            patch.object(schedule.manager, "broadcast", new=AsyncMock()),
            patch.object(
                schedule,
                "with_band_vibration_attachment",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await manager._save_to_chatroom(
                "room-1",
                "connor",
                "Configured Companion查看了监控",
                "reply",
                "assistant-chatroom",
                "[]",
                [],
                system_atts=[snapshot],
            )

        self.assertEqual(
            [snapshot],
            await self._attachments_for(
                "chatroom_messages",
                "sender",
                "system",
            ),
        )
        self.assertEqual(
            [],
            await self._attachments_for(
                "chatroom_messages",
                "sender",
                "connor",
            ),
        )

    async def test_existing_chatroom_monitor_notice_keeps_order_markers_when_snapshot_arrives(self):
        existing = [
            {"type": "system_model_context"},
            {"type": "system_notice_order", "after_msg_id": "assistant-before"},
        ]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO chatroom_messages
                    (id, room_id, sender, content, created_at, attachments)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "monitor-system",
                    "room-1",
                    "system",
                    "Configured Companion查看了监控",
                    1,
                    json.dumps(existing),
                ),
            )
            await db.commit()

        snapshot = {
            "type": "monitor_camera_snapshot",
            "url": "/uploads/arrived.jpg",
        }
        broadcast = AsyncMock()
        with (
            patch.object(
                chatroom_routes,
                "get_db",
                side_effect=self._get_db,
            ),
            patch.object(
                chatroom_routes,
                "broadcast_synced",
                new=broadcast,
            ),
        ):
            updated = await chatroom_routes._append_system_message_attachment(
                "room-1",
                "monitor-system",
                snapshot,
            )

        self.assertEqual(existing + [snapshot], updated["attachments"])
        self.assertEqual(
            existing + [snapshot],
            await self._attachments_for(
                "chatroom_messages",
                "sender",
                "system",
            ),
        )
        self.assertEqual(
            "chatroom_msg_updated",
            broadcast.await_args.args[1]["type"],
        )


if __name__ == "__main__":
    unittest.main()
