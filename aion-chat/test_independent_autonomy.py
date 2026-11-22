import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

import autonomy
import camera


class _DbContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class IndependentAutonomyTests(unittest.IsolatedAsyncioTestCase):
    def test_family_dynamics_shows_independent_countdowns_without_state_packets(self):
        page = (Path(__file__).parent / "static" / "family-dynamics.html").read_text(encoding="utf-8")

        self.assertIn("下次随机唤醒", page)
        self.assertNotIn("当前状态：", page)
        self.assertNotIn("状态包历史", page)
        self.assertNotIn("醒来目的：", page)

    def test_memory_browse_and_rest_are_registered(self):
        self.assertIn("memory_browse", autonomy.ACTION_DEFS)
        self.assertIn("rest", autonomy.ACTION_DEFS)

    def test_monitor_check_is_not_a_second_autonomy_action(self):
        self.assertNotIn("cam_check", autonomy.ACTION_DEFS)

    def test_internal_wake_event_is_hidden_from_family_timeline(self):
        row = {
            "action": "cam_check", "result_type": "", "result_id": "",
            "metadata": '{"autonomy_wake_internal":true}', "actor": "aion", "title": "查看了监控",
        }
        self.assertIsNone(autonomy._idle_event_home_title(row, set(), set()))

    def test_old_private_chat_wake_summary_uses_objective_timeline_title(self):
        from routes import activity

        row = {
            "action": "wake_summary", "result_type": "", "result_id": "",
            "metadata": '{"selected_action":"private_chat"}',
            "title": "醒来后回来报了平安",
            "detail": "",
        }

        self.assertEqual(
            "Connor说了一句话",
            activity._idle_event_timeline_title(row, "Connor", set(), set()),
        )

    def test_role_chat_wake_summary_is_hidden_because_action_is_already_recorded(self):
        from routes import activity

        row = {
            "action": "wake_summary", "result_type": "", "result_id": "",
            "metadata": '{"selected_action":"role_chat"}',
            "title": "又写了一条重复总结",
            "detail": "",
        }

        self.assertIsNone(
            activity._idle_event_timeline_title(row, "Aion", set(), set()),
        )

    def test_rest_wake_summary_remains_visible(self):
        from routes import activity

        row = {
            "action": "wake_summary", "result_type": "", "result_id": "",
            "metadata": '{"selected_action":"rest"}',
            "title": "Connor觉得没什么好做，决定继续休息",
            "detail": "",
        }

        self.assertEqual(
            "Connor觉得没什么好做，决定继续休息",
            activity._idle_event_timeline_title(row, "Connor", set(), set()),
        )

    def test_xhs_and_friend_visit_events_remain_visible_in_family_timeline(self):
        from routes import activity

        for action, title in (
            ("xhs_roam", "Aion去小红书逛了一圈"),
            ("friend_visit_completed", "Connor拜访了朋友"),
            ("friend_visit_interrupted", "Connor的拜访中途结束了"),
        ):
            with self.subTest(action=action):
                row = {
                    "action": action, "result_type": "", "result_id": "",
                    "metadata": "{}", "title": title, "detail": "",
                }
                self.assertEqual(
                    title,
                    activity._idle_event_timeline_title(row, "Connor", set(), set()),
                )

    async def test_actor_action_switches_are_read_independently(self):
        async def config(actor):
            actions = {key: False for key in autonomy.ACTION_DEFS if key != "rest"}
            actions["web_roam"] = actor == "connor"
            return {"enabled": True, "actions": actions}

        with patch.object(autonomy, "get_actor_config", new=config), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={"action": "web_roam"})):
            aion = await autonomy._select_action("aion", manual=True)
            connor = await autonomy._select_action("connor", manual=True)

        self.assertEqual("rest", aion["action"])
        self.assertEqual("web_roam", connor["action"])

    async def test_action_prompt_reports_how_long_user_has_been_silent(self):
        prompts = []

        async def ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "rest"}

        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={
            "enabled": True,
            "actions": {key: False for key in autonomy.ACTION_DEFS if key != "rest"},
        })), patch.object(autonomy, "_ask_actor_json", new=ask):
            await autonomy._select_action("aion", idle_minutes=95)

        self.assertIn("用户已经 1 小时 35 分钟没有发送新消息", prompts[0])
        self.assertNotIn("状态包", prompts[0])

    async def test_disabled_rest_is_not_offered_but_bad_model_responses_still_fall_back(self):
        cfg = {"actions": {"rest": False, "home_dynamics": True}}
        for response in ({"action": "home_dynamics"}, {}, None, [], {"action": "rest"}, RuntimeError("offline")):
            ask = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
            with self.subTest(response=response), \
                 patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=cfg)), \
                 patch.object(autonomy, "recent_niche_index", new=AsyncMock(return_value=[])), \
                 patch.object(autonomy, "_ask_actor_json", new=ask):
                result = await autonomy._select_action("connor")
                self.assertNotIn("- rest:", ask.await_args.args[1])
                self.assertIn("- home_dynamics:", ask.await_args.args[1])
                self.assertEqual(result["action"], "home_dynamics" if response == {"action": "home_dynamics"} else "rest")
                self.assertEqual(ask.await_count, 1)

    async def test_no_enabled_action_avoids_model_call(self):
        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={"actions": {"rest": False}})), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={})) as ask:
            result = await autonomy._select_action("connor")
            self.assertEqual(result["action"], "rest")
            ask.assert_not_awaited()

    async def test_run_actor_does_not_touch_other_actor(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "aion", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "defer_actor_wake", new=AsyncMock()) as defer, \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(return_value={"ok": True, "actor": "aion", "action": "rest", "result": {}})) as run, \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("aion", manual=False)

        self.assertTrue(result["ok"])
        defer.assert_not_awaited()
        run.assert_awaited_once_with("aion", manual=False, idle_minutes=16, wake_id="1300")
        schedule.assert_awaited_once_with("aion", anchor_at=2_000)

    async def test_enabled_actor_without_a_timer_gets_an_initial_countdown(self):
        manager = autonomy.IdleAutonomyManager()
        config = {"enabled": True, "timer_started_at": None, "next_wake_at": None}
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=config)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=0)), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock(return_value={
                 **config, "timer_started_at": 2_000, "next_wake_at": 5_600,
             })) as schedule, \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock()) as claim:
            result = await manager.run_actor_once("connor", manual=False)

        self.assertEqual("not due", result["skipped"])
        schedule.assert_awaited_once_with("connor", anchor_at=2_000)
        claim.assert_not_awaited()

    async def test_wake_action_decision_has_no_state_packet_context(self):
        model_calls = []

        async def call_actor(_actor, messages):
            model_calls.append(messages)
            return '{"action":"rest"}'

        with patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_call_actor", new=call_actor):
            await autonomy._ask_actor_json("connor", "选择本次行动")

        prompt = "\n".join(message["content"] for message in model_calls[0])
        self.assertNotIn("状态包", prompt)

    async def test_wake_action_decision_receives_one_monitor_snapshot(self):
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(
                status="ready",
                source="camera",
                jpeg=b"combined",
                camera_jpeg=b"camera",
            ),
            prompt_attachment="/uploads/autonomy-aion.jpg",
            snapshot_attachment={"type": "monitor_camera_snapshot", "url": "/uploads/camera.jpg"},
        )
        model_calls = []

        async def call_actor(_actor, messages):
            model_calls.append(messages)
            return '{"action":"rest"}'

        token = autonomy._AUTONOMY_WAKE_MONITOR_CONTEXT.set(monitor_context)
        try:
            with patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
                 patch.object(autonomy, "_call_actor", new=call_actor):
                await autonomy._ask_actor_json("aion", "选择本次行动")
        finally:
            autonomy._AUTONOMY_WAKE_MONITOR_CONTEXT.reset(token)

        monitor_messages = [
            message for message in model_calls[0]
            if "/uploads/autonomy-aion.jpg" in message.get("attachments", [])
        ]
        self.assertEqual(1, len(monitor_messages))
        self.assertIn("本次自主醒来实时环境", monitor_messages[0]["content"])
        self.assertNotIn("现实变化可以让你放弃原计划", monitor_messages[0]["content"])
        self.assertIn("不影响你安静做自己的事", monitor_messages[0]["content"])

    async def test_due_wake_captures_and_saves_monitor_card_before_action(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "aion", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        order = []

        async def capture(_actor):
            order.append("capture")
            return monitor_context

        async def save_card(_actor, _context):
            order.append("card")

        async def run(_actor, *, manual=False, idle_minutes=0, wake_id=""):
            order.append("action")
            self.assertIs(monitor_context, autonomy._AUTONOMY_WAKE_MONITOR_CONTEXT.get())
            return {"ok": True, "actor": _actor, "action": "rest", "result": {}}

        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=capture), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=save_card), \
             patch.object(autonomy, "_run_actor_once", new=run), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()):
            result = await manager.run_actor_once("aion", manual=False)

        self.assertTrue(result["ok"])
        self.assertEqual(["capture", "card", "action"], order)
        self.assertIsNone(autonomy._AUTONOMY_WAKE_MONITOR_CONTEXT.get())

    async def test_disable_after_claim_stops_before_monitor_capture_and_action(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "aion", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(
                 autonomy,
                 "get_actor_config",
                 new=AsyncMock(side_effect=[timer, {"enabled": False}]),
             ), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock()) as capture, \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock()) as run, \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()):
            result = await manager.run_actor_once("aion", manual=False)

        self.assertEqual("disabled", result["skipped"])
        capture.assert_not_awaited()
        run.assert_not_awaited()

    async def test_failed_wake_still_starts_the_next_countdown(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "aion", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(side_effect=RuntimeError("action failed"))), \
             patch.object(autonomy, "append_idle_event", new=AsyncMock()), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("aion", manual=False)

        self.assertFalse(result["ok"])
        self.assertEqual("action failed", result["error"])
        schedule.assert_awaited_once_with("aion", anchor_at=2_000)

    async def test_mid_interval_user_chat_does_not_reset_future_wake(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "connor", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 5_000}
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_900)), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule, \
             patch.object(autonomy, "defer_actor_wake", new=AsyncMock()) as defer, \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock()) as claim:
            result = await manager.run_actor_once("connor", manual=False)

        self.assertEqual("not due", result["skipped"])
        schedule.assert_not_awaited()
        defer.assert_not_awaited()
        claim.assert_not_awaited()

    async def test_due_wake_defers_to_chat_quiet_window_when_user_recently_chatted(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "connor", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_950}
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=1_500)), \
             patch.object(autonomy, "defer_actor_wake", new=AsyncMock()) as defer, \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock()) as claim, \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("connor", manual=False)

        expected_until = 1_500 + autonomy.CHAT_QUIET_MINUTES * 60
        self.assertEqual("active_chat_deferred", result["skipped"])
        self.assertEqual(expected_until, result["deferred_until"])
        defer.assert_awaited_once_with("connor", wake_at=expected_until)
        claim.assert_not_awaited()
        schedule.assert_not_awaited()

    async def test_actor_chat_timestamps_are_isolated_for_private_and_shared_for_group(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE messages(id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL);
                CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY, room_id TEXT, sender TEXT, content TEXT, created_at REAL);
                INSERT INTO chatroom_rooms VALUES('c1', 'connor_1v1', 0), ('g1', 'group', 0);
                INSERT INTO messages VALUES('m1', 'conv', 'user', '私聊Aion', 2000);
            """)
            with patch.object(autonomy, "get_db", side_effect=lambda: _DbContext(db)):
                self.assertEqual(2_000, await autonomy._latest_user_message_ts_for_actor("aion"))
                self.assertEqual(0.0, await autonomy._latest_user_message_ts_for_actor("connor"))
                await db.execute("INSERT INTO chatroom_messages VALUES('cm1', 'c1', 'user', '私聊Connor', 2500)")
                self.assertEqual(2_000, await autonomy._latest_user_message_ts_for_actor("aion"))
                self.assertEqual(2_500, await autonomy._latest_user_message_ts_for_actor("connor"))
                await db.execute("INSERT INTO chatroom_messages VALUES('cm2', 'g1', 'user', '群里喊一声', 3000)")
                self.assertEqual(3_000, await autonomy._latest_user_message_ts_for_actor("aion"))
                self.assertEqual(3_000, await autonomy._latest_user_message_ts_for_actor("connor"))

    async def test_overnight_wake_schedules_next_wake_using_configured_interval(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "connor", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 9_900}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        with patch.object(autonomy.time, "time", return_value=10_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts_for_actor", new=AsyncMock(return_value=0.0)), \
             patch.object(autonomy, "defer_actor_wake", new=AsyncMock()) as defer, \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(return_value={
                 "ok": True, "actor": "connor", "action": "web_roam", "result": {},
             })), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("connor", manual=False)

        self.assertTrue(result["ok"])
        defer.assert_not_awaited()
        schedule.assert_awaited_once_with("connor", anchor_at=10_000)

    async def test_rest_enabled_always_at_end_and_disabled_omitted(self):
        prompts = []

        async def ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "web_roam", "reason": "逛逛"}

        base_actions = {
            "private_chat": True,
            "role_chat": True,
            "peer_dm": True,
            "home_dynamics": True,
            "memory_browse": True,
            "web_roam": True,
            "seeky_interaction": True,
        }
        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={"actions": {**base_actions, "rest": True}})), \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=True), \
             patch.object(autonomy, "_recent_actor_actions", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "recent_niche_index", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_ask_actor_json", new=ask):
            await autonomy._select_action("connor", manual=False)

        self.assertIn("- rest:", prompts[0])
        option_lines = [line for line in prompts[0].splitlines() if line.startswith("- ")]
        self.assertTrue(option_lines[-1].startswith("- rest:"))

        prompts.clear()
        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={"actions": {**base_actions, "rest": False}})), \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=True), \
             patch.object(autonomy, "_recent_actor_actions", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "recent_niche_index", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_ask_actor_json", new=ask):
            await autonomy._select_action("connor", manual=False)

        self.assertNotIn("- rest:", prompts[0])

    async def test_weighted_sampling_limits_automatic_menu_and_avoids_back_to_back_repeat(self):
        enabled = [
            "rest", "private_chat", "role_chat", "web_roam", "book_read",
            "home_dynamics", "memory_browse", "seeky_interaction", "peer_dm",
        ]
        recent = [
            {"action": "book_read", "title": "Connor读了一段书", "created_at": 1_700_000_000},
            {"action": "memory_browse", "title": "Connor查看了记忆库", "created_at": 1_699_990_000},
        ]
        for _ in range(20):
            auto_offered = autonomy._sample_action_candidates(enabled, recent, manual=False)
            non_rest = [item for item in auto_offered if item != "rest"]
            self.assertEqual("rest", auto_offered[-1])
            self.assertEqual(5, len(non_rest))
            self.assertEqual(5, len(set(non_rest)))
            self.assertIn("role_chat", non_rest)
            self.assertNotIn("book_read", non_rest)
            quiet_count = sum(1 for item in non_rest if item in autonomy.QUIET_ACTIONS)
            interactive_count = sum(1 for item in non_rest if item in autonomy.INTERACTIVE_ACTIONS)
            self.assertEqual(3, quiet_count)
            self.assertEqual(2, interactive_count)

        manual_offered = autonomy._sample_action_candidates(enabled, recent, manual=True)
        self.assertEqual(set(enabled), set(manual_offered))

        prompts = []
        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={"actions": {
            "rest": True, "web_roam": True, "home_dynamics": True,
            "memory_browse": True, "seeky_interaction": True, "private_chat": True, "role_chat": True,
        }})), \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=True), \
             patch.object(autonomy, "_recent_actor_actions", new=AsyncMock(return_value=recent)), \
             patch.object(autonomy, "recent_niche_index", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock(side_effect=lambda _a, p: (prompts.append(p), {"action": "web_roam"})[1])):
            await autonomy._select_action("connor", manual=False)

        self.assertIn("你最近几次自主行动", prompts[0])
        self.assertIn("Connor读了一段书", prompts[0])

    async def test_role_chat_is_always_offered_when_enabled_without_crowding_out_other_actions(self):
        quiet_actions = [
            "web_roam", "book_read", "home_dynamics", "memory_browse", "seeky_interaction",
        ]
        recent = [{"action": "role_chat", "title": "Connor在群聊里找Aion主动聊了起来", "created_at": 1_700_000_000}]

        enabled_with_others = ["rest", "private_chat", "wish_pool", "role_chat", *quiet_actions]
        for _ in range(20):
            offered = autonomy._sample_action_candidates(enabled_with_others, recent, manual=False)
            non_rest = [item for item in offered if item != "rest"]
            self.assertEqual("rest", offered[-1])
            self.assertEqual(5, len(non_rest))
            self.assertEqual(5, len(set(non_rest)))
            self.assertIn("role_chat", non_rest)
            self.assertEqual(3, sum(1 for item in non_rest if item in autonomy.QUIET_ACTIONS))
            self.assertEqual(2, sum(1 for item in non_rest if item in autonomy.INTERACTIVE_ACTIONS))
            self.assertTrue("private_chat" in non_rest or "wish_pool" in non_rest)

        enabled_role_chat_only = ["rest", "role_chat", *quiet_actions]
        for _ in range(10):
            offered = autonomy._sample_action_candidates(enabled_role_chat_only, recent, manual=False)
            non_rest = [item for item in offered if item != "rest"]
            self.assertEqual("rest", offered[-1])
            self.assertEqual(4, len(non_rest))
            self.assertEqual(4, len(set(non_rest)))
            self.assertIn("role_chat", non_rest)
            self.assertEqual(3, sum(1 for item in non_rest if item in autonomy.QUIET_ACTIONS))
            self.assertEqual(1, sum(1 for item in non_rest if item in autonomy.INTERACTIVE_ACTIONS))

        disabled_role_chat = ["rest", "private_chat", "wish_pool", *quiet_actions]
        for _ in range(20):
            offered = autonomy._sample_action_candidates(disabled_role_chat, recent, manual=False)
            non_rest = [item for item in offered if item != "rest"]
            self.assertEqual("rest", offered[-1])
            self.assertEqual(4, len(non_rest))
            self.assertNotIn("role_chat", non_rest)
            self.assertEqual(3, sum(1 for item in non_rest if item in autonomy.QUIET_ACTIONS))
            self.assertEqual(1, sum(1 for item in non_rest if item in autonomy.INTERACTIVE_ACTIONS))


if __name__ == "__main__":
    unittest.main()
