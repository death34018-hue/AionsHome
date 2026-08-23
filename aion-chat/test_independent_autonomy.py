import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import autonomy
import camera


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

    async def test_run_actor_does_not_touch_other_actor(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "aion", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        with patch.object(autonomy.time, "time", return_value=1_600), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(return_value={"ok": True, "actor": "aion", "action": "rest", "result": {}})) as run, \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("aion", manual=False)

        self.assertTrue(result["ok"])
        run.assert_awaited_once_with("aion", manual=False, idle_minutes=10)
        schedule.assert_awaited_once_with("aion", anchor_at=1_600)

    async def test_enabled_actor_without_a_timer_gets_an_initial_countdown(self):
        manager = autonomy.IdleAutonomyManager()
        config = {"enabled": True, "timer_started_at": None, "next_wake_at": None}
        with patch.object(autonomy.time, "time", return_value=2_000), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=config)), \
             patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(return_value=0)), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
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

        async def run(_actor, *, manual=False, idle_minutes=0):
            order.append("action")
            self.assertIs(monitor_context, autonomy._AUTONOMY_WAKE_MONITOR_CONTEXT.get())
            return {"ok": True, "actor": _actor, "action": "rest", "result": {}}

        with patch.object(autonomy.time, "time", return_value=1_600), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
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
        with patch.object(
            autonomy,
            "get_actor_config",
            new=AsyncMock(side_effect=[timer, {"enabled": False}]),
        ), patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
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
        with patch.object(autonomy.time, "time", return_value=1_600), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(return_value=1_000)), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(side_effect=RuntimeError("action failed"))), \
             patch.object(autonomy, "append_idle_event", new=AsyncMock()), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            result = await manager.run_actor_once("aion", manual=False)

        self.assertFalse(result["ok"])
        self.assertEqual("action failed", result["error"])
        schedule.assert_awaited_once_with("aion", anchor_at=1_600)

    async def test_user_message_during_wake_becomes_next_timer_anchor(self):
        manager = autonomy.IdleAutonomyManager()
        timer = {"actor": "connor", "enabled": True, "timer_started_at": 1_000, "next_wake_at": 1_300}
        monitor_context = camera.MonitorPromptContext(
            image_result=camera.MonitorImageResult(status="unavailable", source="device"),
        )
        with patch.object(autonomy.time, "time", return_value=1_600), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_latest_user_message_ts", new=AsyncMock(side_effect=[1_000, 1_500])), \
             patch.object(autonomy, "refresh_actor_wake_for_user", new=AsyncMock(return_value=False)), \
             patch.object(autonomy, "claim_due_wake", new=AsyncMock(return_value=timer)), \
             patch.object(autonomy, "_capture_autonomy_monitor_context", new=AsyncMock(return_value=monitor_context)), \
             patch.object(autonomy, "_save_autonomy_monitor_card", new=AsyncMock()), \
             patch.object(autonomy, "_run_actor_once", new=AsyncMock(return_value={
                 "ok": True, "actor": "connor", "action": "rest", "result": {},
             })), \
             patch.object(autonomy, "schedule_actor_wake", new=AsyncMock()) as schedule:
            await manager.run_actor_once("connor", manual=False)

        schedule.assert_awaited_once_with("connor", anchor_at=1_500)


if __name__ == "__main__":
    unittest.main()
