import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock


BASE_DIR = pathlib.Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import capabilities
import aiosqlite
import phone_screen
import schedule
from app_supervision_ai import (
    AppSupervisionStateCache,
    build_app_supervision_ability_text,
    parse_app_supervision_command,
    ensure_app_supervision_tables,
    enqueue_app_supervision_command,
    list_pending_app_supervision_commands,
    expire_pending_app_supervision_commands,
    acknowledge_app_supervision_command,
    format_app_supervision_result_message,
    inject_app_supervision_context,
)
from routes import app_supervision as app_supervision_routes
from web_search import WebCommandStreamFilter


class AppSupervisionCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.cache = AppSupervisionStateCache()

    def test_capability_catalog_contains_stable_toggle(self):
        item = capabilities.get_capability_def("app_supervision")
        self.assertIsNotNone(item)
        self.assertEqual("app_supervision", item.key)

    def test_disabled_capability_injects_no_prompt_or_state(self):
        self.cache.replace_snapshot(
            {
                "groups": [
                    {
                        "groupId": "xhs",
                        "displayName": "小红书",
                        "roundUsageMs": 20 * 60_000,
                        "foregroundOpen": True,
                    }
                ]
            },
            received_at=1_000.0,
        )
        with patch("app_supervision_ai.is_capability_enabled", return_value=False):
            self.assertEqual("", build_app_supervision_ability_text(self.cache, now=1_060.0))

    def test_enabled_prompt_renders_commands_mapping_and_cached_estimate(self):
        self.cache.replace_snapshot(
            {
                "deviceLock": {
                    "effectiveState": "LOCKED",
                    "lock": {
                        "deadlineWallMs": 1_120_000,
                        "roleId": "connor",
                        "message": "该睡觉了",
                    },
                    "temporaryUnlock": None,
                },
                "groups": [
                    {
                        "groupId": "xhs-main",
                        "displayName": "小红书",
                        "roundUsageMs": 20 * 60_000,
                        "foregroundOpen": True,
                        "effectiveState": "NORMAL",
                    },
                    {
                        "groupId": "douyin-main",
                        "displayName": "抖音",
                        "roundUsageMs": 5 * 60_000,
                        "foregroundOpen": False,
                        "effectiveState": "LOCKED",
                    },
                ]
            },
            received_at=1_000.0,
        )
        with patch("app_supervision_ai.is_capability_enabled", return_value=True):
            text = build_app_supervision_ability_text(self.cache, now=1_060.0)
        self.assertIn("[APP_LOCK:groupId|分钟|锁屏提示]", text)
        self.assertIn("[APP_TEMP_UNLOCK:groupId|分钟|解锁说明]", text)
        self.assertIn("[APP_UNLOCK:groupId]", text)
        self.assertIn("[DEVICE_LOCK:分钟|锁屏提示]", text)
        self.assertIn("[DEVICE_TEMP_UNLOCK:分钟|解锁说明]", text)
        self.assertIn("[DEVICE_UNLOCK]", text)
        self.assertIn("整机状态 LOCKED", text)
        self.assertIn("负责角色 connor", text)
        self.assertIn("该睡觉了", text)
        self.assertIn("连续切换", text)
        self.assertIn("xhs-main → 小红书", text)
        self.assertIn("douyin-main → 抖音", text)
        self.assertIn("21.0 分钟（估算）", text)
        self.assertIn("5.0 分钟", text)
        self.assertIn("快照于 60 秒前", text)

    def test_capability_prompt_builder_uses_in_memory_cache_synchronously(self):
        sentinel = "cached supervision text"
        with (
            patch("capabilities.is_capability_enabled", side_effect=lambda key: key == "app_supervision"),
            patch("app_supervision_ai.build_app_supervision_ability_text", return_value=sentinel) as build,
        ):
            items = asyncio.run(capabilities.build_capability_prompt_items("User X"))
        self.assertIn(sentinel, items)
        build.assert_called_once()

    def test_passive_context_injection_is_shared_and_does_not_duplicate(self):
        messages = [{"role": "assistant", "content": "recent"}, {"role": "user", "content": "trigger"}]
        with patch("app_supervision_ai.build_app_supervision_ability_text", return_value="[APP_LOCK:x]"):
            inject_app_supervision_context(messages)
            inject_app_supervision_context(messages)
        self.assertEqual(1, sum("APP_LOCK" in item["content"] for item in messages))
        self.assertEqual("trigger", messages[-1]["content"])


class AppSupervisionCommandParserTests(unittest.TestCase):
    def test_three_commands_parse_and_are_removed_from_visible_text(self):
        cases = [
            ("[APP_LOCK:xhs|60|去休息]正文", "lock", 60, "去休息"),
            ("正文[APP_TEMP_UNLOCK:xhs|15|回复消息]", "temp_unlock", 15, "回复消息"),
            ("[APP_UNLOCK:xhs]正文", "unlock", None, ""),
        ]
        for source, action, minutes, message in cases:
            with self.subTest(source=source):
                cleaned, command = parse_app_supervision_command(
                    source, valid_group_ids={"xhs"}, enabled=True
                )
                self.assertEqual("正文", cleaned)
                self.assertEqual(action, command["action"])
                self.assertEqual(minutes, command.get("minutes"))
                self.assertEqual(message, command.get("message", ""))

    def test_only_first_valid_command_executes_but_all_protocol_text_is_hidden(self):
        cleaned, command = parse_app_supervision_command(
            "好[APP_LOCK:missing|60|x][APP_LOCK:xhs|30|y][APP_UNLOCK:xhs]",
            valid_group_ids={"xhs"},
            enabled=True,
        )
        self.assertEqual("好", cleaned)
        self.assertEqual("lock", command["action"])
        self.assertEqual(30, command["minutes"])

    def test_device_commands_parse_with_empty_group_id(self):
        cases = [
            ("[DEVICE_LOCK:30|去睡觉]", "device_lock", 30, "去睡觉"),
            (
                "[DEVICE_TEMP_UNLOCK:10|处理消息]",
                "device_temp_unlock",
                10,
                "处理消息",
            ),
            ("[DEVICE_UNLOCK]", "device_unlock", None, ""),
        ]
        for source, action, minutes, message in cases:
            with self.subTest(source=source):
                cleaned, command = parse_app_supervision_command(
                    source, valid_group_ids=set(), enabled=True
                )
                self.assertEqual("", cleaned)
                self.assertEqual(action, command["action"])
                self.assertEqual("", command["groupId"])
                self.assertEqual(minutes, command.get("minutes"))
                self.assertEqual(message, command.get("message", ""))

    def test_invalid_device_directives_are_hidden_and_never_execute(self):
        for directive in (
            "[DEVICE_LOCK:0|x]",
            "[DEVICE_LOCK:121|x]",
            "[DEVICE_LOCK:1.5|x]",
            "[DEVICE_LOCK:20]",
            "[DEVICE_LOCK:20|x|extra]",
            "[DEVICE_TEMP_UNLOCK]",
            "[DEVICE_UNLOCK:extra]",
        ):
            with self.subTest(directive=directive):
                cleaned, command = parse_app_supervision_command(
                    f"正文{directive}", {"xhs"}, enabled=True
                )
                self.assertEqual("正文", cleaned)
                self.assertIsNone(command)

    def test_minutes_group_and_disabled_gate_are_enforced(self):
        for minutes in (0, 121, 1.5):
            cleaned, command = parse_app_supervision_command(
                f"正文[APP_LOCK:xhs|{minutes}|x]", {"xhs"}, enabled=True
            )
            self.assertEqual("正文", cleaned)
            self.assertIsNone(command)
        cleaned, command = parse_app_supervision_command(
            "正文[APP_LOCK:xhs|60|x]", {"xhs"}, enabled=False
        )
        self.assertEqual("正文", cleaned)
        self.assertIsNone(command)

    def test_stream_filter_hides_split_commands_from_ui_and_tts(self):
        stream_filter = WebCommandStreamFilter()
        visible = "".join(
            stream_filter.feed(chunk)
            for chunk in ("先休息", "[APP_LO", "CK:xhs|60|去睡觉]", "，好吗")
        ) + stream_filter.flush()
        self.assertEqual("先休息，好吗", visible)

        device_filter = WebCommandStreamFilter()
        device_visible = "".join(
            device_filter.feed(chunk)
            for chunk in ("去睡觉", "[DEVICE_", "LOCK:30|休息]", "。")
        ) + device_filter.flush()
        self.assertEqual("去睡觉。", device_visible)


class AppSupervisionStateRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app_supervision_routes.supervision_state_cache.clear()

    async def test_state_report_updates_cache_and_checkpoint_is_idempotent(self):
        body = app_supervision_routes.StateReport(
            eventId="checkpoint-xhs-20-round-1",
            eventType="checkpoint",
            triggerGroupId="xhs",
            checkpointMinutes=20,
            deviceLock=app_supervision_routes.StateDeviceLock(
                effectiveState="LOCKED",
                lock={"commandId": "device-state-1"},
            ),
            groups=[
                app_supervision_routes.StateGroup(
                    groupId="xhs", displayName="小红书", roleId="connor",
                    roundUsageMs=1_200_000, foregroundOpen=True,
                    effectiveState="NORMAL",
                )
            ],
        )
        wake = AsyncMock()
        with (
            patch("routes.app_supervision.is_capability_enabled", return_value=True),
            patch.object(schedule.schedule_mgr, "fire_app_supervision_checkpoint", wake),
        ):
            first = await app_supervision_routes.report_state(body)
            second = await app_supervision_routes.report_state(body)
            await asyncio.sleep(0)
        self.assertTrue(first["featureEnabled"])
        self.assertTrue(first["checkpointAccepted"])
        self.assertFalse(second["checkpointAccepted"])
        wake.assert_awaited_once()
        wake_group = wake.await_args.args[0]
        self.assertEqual("connor", wake_group["roleId"])
        self.assertEqual(20, wake.await_args.kwargs["checkpoint_minutes"])
        snapshot, _ = app_supervision_routes.supervision_state_cache.read()
        self.assertEqual("xhs", snapshot["groups"][0]["groupId"])
        self.assertEqual(
            "device-state-1", snapshot["deviceLock"]["lock"]["commandId"]
        )

    async def test_disabled_state_route_clears_cache_and_returns_silent_config(self):
        app_supervision_routes.supervision_state_cache.replace_snapshot(
            {"groups": [{"groupId": "old"}]}, received_at=1
        )
        body = app_supervision_routes.StateReport(eventType="enter", groups=[])
        with (
            patch("routes.app_supervision.is_capability_enabled", return_value=False),
            patch("routes.app_supervision.role_catalog", return_value=[
                {"id": "connor", "label": "Configured Connor"},
                {"id": "aion", "label": "Configured Aion"},
            ]),
        ):
            result = await app_supervision_routes.report_state(body)
            config = await app_supervision_routes.get_runtime_config()
        self.assertEqual(
            {"featureEnabled": False, "checkpointAccepted": False}, result
        )
        self.assertEqual(
            {
                "featureEnabled": False,
                "roles": [
                    {"id": "connor", "label": "Configured Connor"},
                    {"id": "aion", "label": "Configured Aion"},
                ],
            },
            config,
        )
        self.assertEqual([], app_supervision_routes.supervision_state_cache.read()[0]["groups"])


class AppSupervisionCommandQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tempdir.name) / "commands.db"
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_app_supervision_tables(db)
            await db.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_pending_command_keeps_id_and_expires_after_five_minutes(self):
        async with aiosqlite.connect(self.db_path) as db:
            created = await enqueue_app_supervision_command(
                db,
                {"action": "lock", "groupId": "xhs", "minutes": 60, "message": "休息"},
                source_message_id="msg-1",
                role_id="connor",
                source_kind="chatroom",
                source_ref="room-1",
                now=1_000.0,
            )
            await db.commit()
            pending = await list_pending_app_supervision_commands(db, now=1_299.9)
            expired_items = await expire_pending_app_supervision_commands(db, now=1_300.1)
            expired = await list_pending_app_supervision_commands(db, now=1_300.1)
            await db.commit()
        self.assertEqual(created["commandId"], pending[0]["commandId"])
        self.assertEqual(1_300.0, created["expiresAt"])
        self.assertEqual(created["commandId"], expired_items[0]["commandId"])
        async with aiosqlite.connect(self.db_path) as db:
            self.assertEqual([], await expire_pending_app_supervision_commands(db, now=1_301))
        self.assertEqual([], expired)

    async def test_source_message_is_idempotent_and_ack_is_idempotent(self):
        command = {"action": "unlock", "groupId": "xhs"}
        async with aiosqlite.connect(self.db_path) as db:
            first = await enqueue_app_supervision_command(
                db, command, source_message_id="msg-1", role_id="aion",
                source_kind="private", source_ref="conv-1", now=1_000,
            )
            second = await enqueue_app_supervision_command(
                db, command, source_message_id="msg-1", role_id="aion",
                source_kind="private", source_ref="conv-1", now=1_001,
            )
            await db.commit()
            accepted = await acknowledge_app_supervision_command(
                db, first["commandId"], success=True, reason="", now=1_010
            )
            duplicate = await acknowledge_app_supervision_command(
                db, first["commandId"], success=True, reason="", now=1_011
            )
            await db.commit()
        self.assertEqual(first["commandId"], second["commandId"])
        self.assertTrue(accepted)
        self.assertFalse(duplicate)

    async def test_device_command_round_trips_with_empty_group(self):
        async with aiosqlite.connect(self.db_path) as db:
            created = await enqueue_app_supervision_command(
                db,
                {
                    "action": "device_lock",
                    "groupId": "",
                    "minutes": 30,
                    "message": "休息",
                },
                source_message_id="device-msg-1",
                role_id="connor",
                source_kind="private",
                source_ref="conv-1",
                now=2_000,
            )
            await db.commit()
            pending = await list_pending_app_supervision_commands(db, now=2_001)
        self.assertEqual("", created["groupId"])
        self.assertEqual("device_lock", pending[0]["action"])


class AppSupervisionCheckpointWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_reuses_alarm_pipeline_with_role_and_explicit_reason(self):
        manager = schedule.ScheduleManager()
        manager._fire_alarm = AsyncMock()
        fresh_path = pathlib.Path("phone_screen_latest.jpg")
        with (
            patch.object(schedule.manager, "broadcast", new=AsyncMock()) as broadcast,
            patch.object(
                phone_screen,
                "wait_for_phone_screen_after",
                new=AsyncMock(return_value=fresh_path),
            ) as wait_for_screen,
            patch.object(
                phone_screen,
                "freeze_phone_screen",
                return_value="/uploads/app_supervision_checkpoint-1.jpg",
            ) as freeze,
        ):
            await manager.fire_app_supervision_checkpoint(
                {"groupId": "xhs", "displayName": "小红书", "roleId": "connor"},
                event_id="checkpoint-1",
                checkpoint_minutes=20,
            )
        item = manager._fire_alarm.await_args.args[0]
        self.assertEqual("connor", item["origin"])
        self.assertTrue(item["_app_supervision_checkpoint"])
        self.assertIn("小红书", item["content"])
        self.assertIn("20 分钟检查点", item["content"])
        self.assertEqual(
            "/uploads/app_supervision_checkpoint-1.jpg",
            item["_app_supervision_phone_attachment"],
        )
        broadcast.assert_awaited_once()
        self.assertEqual("cam_check", broadcast.await_args.args[0]["type"])
        self.assertTrue(broadcast.await_args.args[0]["data"]["capture_only"])
        wait_for_screen.assert_awaited_once()
        freeze.assert_called_once_with(fresh_path, event_id="checkpoint-1")

    async def test_checkpoint_still_wakes_ai_when_phone_capture_times_out(self):
        manager = schedule.ScheduleManager()
        manager._fire_alarm = AsyncMock()
        with (
            patch.object(schedule.manager, "broadcast", new=AsyncMock()),
            patch.object(
                phone_screen,
                "wait_for_phone_screen_after",
                new=AsyncMock(return_value=None),
            ),
            patch.object(phone_screen, "freeze_phone_screen") as freeze,
        ):
            await manager.fire_app_supervision_checkpoint(
                {"groupId": "xhs", "displayName": "小红书", "roleId": "aion"},
                event_id="checkpoint-timeout",
                checkpoint_minutes=40,
            )

        item = manager._fire_alarm.await_args.args[0]
        self.assertNotIn("_app_supervision_phone_attachment", item)
        freeze.assert_not_called()


class AppSupervisionResultMessageTests(unittest.TestCase):
    def test_success_messages_use_configured_role_and_group_names(self):
        roles = {"connor": "Partner X"}
        groups = {"xhs": "小红书"}
        cases = [
            ({"action": "lock", "groupId": "xhs", "minutes": 60}, "【Partner X】锁定了小红书 60 分钟"),
            ({"action": "temp_unlock", "groupId": "xhs", "minutes": 15}, "【Partner X】暂时解锁了小红书 15 分钟"),
            ({"action": "unlock", "groupId": "xhs"}, "【Partner X】解锁了小红书"),
        ]
        for command, expected in cases:
            command["roleId"] = "connor"
            with self.subTest(command=command):
                self.assertEqual(
                    expected,
                    format_app_supervision_result_message(
                        command, success=True, reason="", role_names=roles,
                        group_names=groups,
                    ),
                )

    def test_failure_does_not_claim_success(self):
        text = format_app_supervision_result_message(
            {"action": "lock", "groupId": "xhs", "minutes": 60, "roleId": "aion"},
            success=False,
            reason="命令已过期",
            role_names={"aion": "Main X"},
            group_names={"xhs": "小红书"},
        )
        self.assertEqual("【Main X】未能锁定小红书：命令已过期", text)
        self.assertNotIn("锁定了", text)

    def test_device_success_messages_use_configured_role_name(self):
        cases = [
            (
                {"action": "device_lock", "minutes": 30},
                "【Partner X】锁定了手机 30 分钟",
            ),
            (
                {"action": "device_temp_unlock", "minutes": 10},
                "【Partner X】暂时解锁了手机 10 分钟",
            ),
            ({"action": "device_unlock"}, "【Partner X】解锁了手机"),
        ]
        for command, expected in cases:
            command.update({"groupId": "", "roleId": "connor"})
            with self.subTest(command=command):
                self.assertEqual(
                    expected,
                    format_app_supervision_result_message(
                        command,
                        success=True,
                        reason="",
                        role_names={"connor": "Partner X"},
                        group_names={},
                    ),
                )


if __name__ == "__main__":
    unittest.main()
