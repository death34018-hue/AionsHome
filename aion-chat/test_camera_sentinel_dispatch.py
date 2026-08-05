import tempfile
import unittest
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

import camera


class SentinelDispatchTests(unittest.TestCase):
    def test_fixed_modes_override_sentinel_target(self):
        self.assertEqual(
            ["aion"],
            camera.resolve_sentinel_wake_targets(True, "aion", "both"),
        )
        self.assertEqual(
            ["connor"],
            camera.resolve_sentinel_wake_targets(True, "connor", "aion"),
        )

    def test_smart_both_expands_in_stable_order(self):
        self.assertEqual(
            ["aion", "connor"],
            camera.resolve_sentinel_wake_targets(True, "smart", "both"),
        )

    def test_smart_generic_targets_map_to_internal_actor_ids(self):
        self.assertEqual(
            ["aion"],
            camera.resolve_sentinel_wake_targets(True, "smart", "main_ai"),
        )
        self.assertEqual(
            ["connor"],
            camera.resolve_sentinel_wake_targets(True, "smart", "second_ai"),
        )

    def test_legacy_actor_targets_remain_backward_compatible(self):
        self.assertEqual("main_ai", camera.normalize_sentinel_wake_target("aion"))
        self.assertEqual("second_ai", camera.normalize_sentinel_wake_target("connor"))

    def test_no_core_means_no_targets(self):
        self.assertEqual(
            [],
            camera.resolve_sentinel_wake_targets(False, "smart", "both"),
        )

    def test_invalid_smart_target_falls_back_to_aion(self):
        self.assertEqual(
            ["aion"],
            camera.resolve_sentinel_wake_targets(True, "smart", "unknown"),
        )

    def test_dispatch_policy_uses_configured_names_and_clear_both_boundary(self):
        policy = camera.build_sentinel_dispatch_policy(
            user_name="Configured User",
            ai_name="Configured Main",
            connor_name="Configured Second",
        )

        self.assertIn("Configured Main", policy)
        self.assertIn("Configured Second", policy)
        self.assertIn("main_ai", policy)
        self.assertIn("second_ai", policy)
        self.assertIn("both", policy)
        self.assertNotIn("aion", policy.lower())
        self.assertNotIn("connor", policy.lower())
        self.assertIn("转账", policy)
        self.assertIn("明显危险", policy)
        self.assertIn("普通的不确定", policy)
        self.assertIn("不能", policy)

    def test_parse_dispatch_preserves_sentinel_and_final_targets(self):
        dispatch = camera.parse_sentinel_dispatch(
            {
                "call_core": True,
                "wake_target": "both",
                "wake_reason": "共同关系事件",
                "wake_confidence": "high",
            },
            "smart",
        )

        self.assertEqual("both", dispatch["sentinel_wake_target"])
        self.assertEqual(["aion", "connor"], dispatch["final_wake_targets"])
        self.assertEqual("共同关系事件", dispatch["wake_reason"])
        self.assertEqual("high", dispatch["wake_confidence"])

    def test_parse_dispatch_sanitizes_reason_and_confidence(self):
        dispatch = camera.parse_sentinel_dispatch(
            {
                "call_core": True,
                "wake_target": "invalid",
                "wake_reason": 123,
                "wake_confidence": "certain",
            },
            "smart",
        )

        self.assertEqual("main_ai", dispatch["sentinel_wake_target"])
        self.assertEqual(["aion"], dispatch["final_wake_targets"])
        self.assertEqual("123", dispatch["wake_reason"])
        self.assertEqual("low", dispatch["wake_confidence"])

    def test_sentinel_json_example_uses_generic_target_key(self):
        source = inspect.getsource(camera.CameraMonitor._analyze_and_log)
        self.assertIn('"wake_target":"main_ai"', source)
        self.assertNotIn('"wake_target":"aion"', source)


class CameraActorRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "dispatch.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE chatroom_rooms (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                INSERT INTO conversations (id, updated_at) VALUES ('main-private', 30);
                INSERT INTO chatroom_rooms (id, type, updated_at) VALUES
                    ('group-1', 'group', 20),
                    ('connor-private', 'connor_1v1', 40);
                INSERT INTO messages VALUES
                    ('main-user', 'main-private', 'user', 'main private line', 10);
                INSERT INTO chatroom_messages VALUES
                    ('group-user', 'group-1', 'user', 'group line', 20),
                    ('connor-user', 'connor-private', 'user', 'second private line', 30);
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

    async def test_aion_resolves_private_and_group_windows(self):
        monitor = camera.CameraMonitor()
        with (
            patch.object(camera, "get_db", side_effect=self._get_db),
            patch.object(camera.manager, "get_aion_last_active", return_value="private"),
        ):
            private_target = await monitor._resolve_actor_target("aion")

        with (
            patch.object(camera, "get_db", side_effect=self._get_db),
            patch.object(
                camera.manager,
                "get_aion_last_active",
                return_value="chatroom:group-1",
            ),
        ):
            group_target = await monitor._resolve_actor_target("aion")

        self.assertEqual(
            {"type": "private", "conv_id": "main-private"},
            private_target,
        )
        self.assertEqual(
            {"type": "chatroom", "room_id": "group-1", "room_type": "group"},
            group_target,
        )

    async def test_connor_uses_active_room_and_falls_back_to_private_room(self):
        monitor = camera.CameraMonitor()
        with (
            patch.object(camera, "get_db", side_effect=self._get_db),
            patch.object(
                camera.manager,
                "get_connor_last_active",
                return_value="group-1",
            ),
        ):
            group_target = await monitor._resolve_actor_target("connor")

        with (
            patch.object(camera, "get_db", side_effect=self._get_db),
            patch.object(
                camera.manager,
                "get_connor_last_active",
                return_value="deleted-room",
            ),
        ):
            fallback_target = await monitor._resolve_actor_target("connor")

        self.assertEqual(
            {"type": "chatroom", "room_id": "group-1", "room_type": "group"},
            group_target,
        )
        self.assertEqual(
            {
                "type": "chatroom",
                "room_id": "connor-private",
                "room_type": "connor_1v1",
            },
            fallback_target,
        )

    async def test_both_continues_when_one_actor_fails(self):
        monitor = camera.CameraMonitor()
        calls = []

        async def resolve(actor):
            return {
                "type": "chatroom",
                "room_id": f"{actor}-room",
                "room_type": "connor_1v1" if actor == "connor" else "group",
            }

        async def wake(actor, target, *args, **kwargs):
            calls.append((actor, target["room_id"]))
            if actor == "aion":
                raise RuntimeError("main failed")

        with (
            patch.object(monitor, "_resolve_actor_target", side_effect=resolve),
            patch.object(monitor, "_call_core_for_actor", side_effect=wake),
        ):
            results = await monitor._wake_core_targets(
                ["aion", "connor"],
                "trigger",
                1.0,
            )

        self.assertCountEqual(
            [("aion", "aion-room"), ("connor", "connor-room")],
            calls,
        )
        self.assertFalse(results["aion"]["ok"])
        self.assertTrue(results["connor"]["ok"])

    async def test_same_group_runs_in_stable_actor_order(self):
        monitor = camera.CameraMonitor()
        order = []
        target = {"type": "chatroom", "room_id": "group-1", "room_type": "group"}

        async def wake(actor, _target, *args, **kwargs):
            order.append(actor)

        with (
            patch.object(monitor, "_resolve_actor_target", new=AsyncMock(return_value=target)),
            patch.object(monitor, "_call_core_for_actor", side_effect=wake),
        ):
            await monitor._wake_core_targets(["aion", "connor"], "trigger", 1.0)

        self.assertEqual(["aion", "connor"], order)

    async def test_connor_actor_uses_dedicated_core_path(self):
        monitor = camera.CameraMonitor()
        target = {
            "type": "chatroom",
            "room_id": "connor-private",
            "room_type": "connor_1v1",
        }
        with patch.object(
            monitor,
            "_call_connor_core",
            new=AsyncMock(return_value="reply"),
        ) as connor_core:
            result = await monitor._call_core_for_actor(
                "connor",
                target,
                "trigger",
                1.0,
            )

        self.assertEqual("reply", result)
        connor_core.assert_awaited_once_with(target, "trigger", 1.0)

    async def test_sentinel_recent_context_includes_second_ai_private_window(self):
        with patch.object(camera, "get_db", side_effect=self._get_db):
            text = await camera.async_get_recent_aion_timeline_text(
                "main-private",
                10,
                user_name="Configured User",
                ai_name="Configured Main",
                connor_name="Configured Second",
            )

        self.assertIn("second private line", text)
        self.assertIn("Configured User", text)

    async def test_sentinel_last_user_time_includes_second_ai_private_window(self):
        with patch.object(camera, "get_db", side_effect=self._get_db):
            latest = await camera.async_get_last_aion_timeline_user_msg_time(
                "main-private"
            )

        self.assertEqual(30.0, latest)


if __name__ == "__main__":
    unittest.main()
