import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonomy
import camera
import ai_providers


class _FakeCursor:
    async def fetchone(self):
        return None


class _RecordingDb:
    def __init__(self):
        self.statements = []

    async def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return _FakeCursor()

    async def commit(self):
        return None


class _DbContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class IdleAutonomyActionTests(unittest.TestCase):
    def test_memory_browse_is_registered_as_an_idle_autonomy_choice(self):
        stale_settings = {
            "idle_autonomy_actions": {
                "memory_browse": True,
                "home_dynamics": True,
            }
        }

        with patch.object(autonomy, "SETTINGS", stale_settings):
            cfg = autonomy.get_idle_config()

        self.assertIn("memory_browse", autonomy.ACTION_DEFS)
        self.assertTrue(cfg["actions"]["memory_browse"])
        self.assertIn("home_dynamics", cfg["actions"])
        self.assertIn("web_roam", autonomy.ACTION_DEFS)
        self.assertIn("web_roam", cfg["actions"])

    def test_friend_visit_is_an_idle_autonomy_choice(self):
        self.assertEqual(autonomy.ACTION_DEFS["friend_visit"], "拜访一位 AI 好友")

    def test_saving_legacy_idle_config_keeps_registered_memory_browse_setting(self):
        stale_settings = {
            "idle_autonomy_actions": {
                "memory_browse": True,
                "home_dynamics": True,
            }
        }

        with patch.object(autonomy, "SETTINGS", stale_settings), \
             patch.object(autonomy, "save_settings") as save_settings:
            cfg = autonomy.save_idle_config(actions={"home_dynamics": False})

        self.assertTrue(cfg["actions"]["memory_browse"])
        self.assertTrue(stale_settings["idle_autonomy_actions"]["memory_browse"])
        save_settings.assert_called_once_with(stale_settings)


class SharedMonitorContextTests(unittest.IsolatedAsyncioTestCase):
    def test_model_attachment_resolves_screenshots_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(ai_providers, "SCREENSHOTS_DIR", Path(temp_dir)):
            resolved = ai_providers._resolve_attachment_path("/screenshots/monitor_test.jpg")

        self.assertEqual(Path(temp_dir) / "monitor_test.jpg", resolved)

    async def test_shared_context_plays_one_alert_and_returns_prompt_and_card_attachments(self):
        image_result = camera.MonitorImageResult(
            status="ready",
            source="phone",
            jpeg=b"combined-jpeg",
            camera_jpeg=b"camera-jpeg",
            request_id="request-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(camera, "SCREENSHOTS_DIR", root / "screenshots"), \
                 patch.dict(camera.cam.cfg, {"active_source": "phone"}), \
                 patch.object(camera.manager, "broadcast", new=AsyncMock()) as broadcast, \
                 patch.object(camera, "acquire_monitor_image", new=AsyncMock(return_value=image_result)) as acquire:
                context = await camera.acquire_prompt_monitor_context(
                    "autonomy_wake",
                    alert_content="自主唤醒查看",
                    alert_extra={"origin": "aion"},
                )

                self.assertFalse((root / "uploads").exists())
                saved = list((root / "screenshots").glob("*.jpg"))
                self.assertEqual(2, len(saved))

        acquire.assert_awaited_once_with("autonomy_wake")
        broadcast.assert_awaited_once()
        alert = broadcast.await_args.args[0]
        self.assertEqual("monitor_alert", alert["type"])
        self.assertEqual("自主唤醒查看", alert["data"]["content"])
        self.assertEqual("aion", alert["data"]["origin"])
        self.assertTrue(alert["data"]["phone_camera_native_capture"])
        self.assertEqual(image_result, context.image_result)
        self.assertTrue(context.prompt_attachment.startswith("/screenshots/monitor_autonomy_wake_"))
        self.assertEqual("monitor_camera_snapshot", context.snapshot_attachment["type"])
        self.assertTrue(context.snapshot_attachment["url"].startswith("/screenshots/monitor_camera_autonomy_wake_"))

    async def test_shared_monitor_retention_keeps_only_configured_newest_images(self):
        image_result = camera.MonitorImageResult(
            status="ready", source="phone", jpeg=b"combined", camera_jpeg=b"camera"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            screenshots = root / "screenshots"
            screenshots.mkdir()
            for filename in (
                "cam_20200101_000000.jpg",
                "monitor_20200101_000001.jpg",
                "phone_camera_old.jpg",
            ):
                old = screenshots / filename
                old.write_bytes(b"old")
                old.touch()
            with patch.object(camera, "SCREENSHOTS_DIR", screenshots), \
                 patch.dict(camera.cam.cfg, {"active_source": "phone", "max_screenshots": 3}), \
                 patch.object(camera.manager, "broadcast", new=AsyncMock()), \
                 patch.object(camera, "acquire_monitor_image", new=AsyncMock(return_value=image_result)):
                await camera.acquire_prompt_monitor_context(
                    "autonomy_wake", alert_content="自主唤醒查看"
                )

            self.assertEqual(3, len(list(screenshots.glob("*.jpg"))))
            self.assertFalse((root / "uploads").exists())


class IdleAutonomyWebRoamTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_action_filters_web_roam_when_web_search_unavailable(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "web_roam", "reason": "想搜点新鲜内容"}

        with patch.object(autonomy, "get_idle_config", return_value={
            "actions": {"web_roam": True},
        }), \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=False), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("aion")

        self.assertEqual(selected["action"], "rest")
        self.assertTrue(prompts)
        self.assertNotIn("web_roam", prompts[0])
        self.assertNotIn("上网冲浪", prompts[0])

    async def test_truncated_or_unknown_action_defaults_to_rest(self):
        config = {
            "enabled": True,
            "actions": {"private_chat": True, "home_dynamics": True},
        }
        for response in ({}, {"action": "private_chat_but_truncated"}):
            with self.subTest(response=response), \
                 patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=config)), \
                 patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value=response)):
                selected = await autonomy._select_action("aion")

            self.assertEqual("rest", selected["action"])

    async def test_manual_select_action_keeps_web_roam_so_unavailable_reason_is_visible(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "web_roam", "reason": "按用户要求测试上网冲浪"}

        with patch.object(autonomy, "get_idle_config", return_value={
            "actions": {"web_roam": True, "home_dynamics": True},
        }), \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=False), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("aion", manual=True)

        self.assertEqual(selected["action"], "web_roam")
        self.assertIn("web_roam", prompts[0])
        self.assertIn("明确要求测试", prompts[0])

    async def test_run_web_roam_searches_replies_and_records_family_activity(self):
        saved_message = {"id": "msg_web", "content": "我刚才搜索了 AI 桌面宠物设计，看到一篇挺有意思：https://example.com"}
        preview = {"type": "link_preview", "url": "https://example.com", "title": "Example"}
        events = []

        async def fake_append(*args, **kwargs):
            events.append((args, kwargs))
            return {"id": "idle_web"}

        with patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={
            "search_command": "[WEB_SEARCH:AI 桌面宠物设计]",
            "reason": "想找点灵感",
        })) as ask_actor, \
             patch.object(autonomy, "_is_idle_web_roam_available", return_value=True), \
             patch.object(autonomy, "run_web_commands", new=AsyncMock(return_value=[
                 "【联网搜索结果】\n查询：AI 桌面宠物设计\n1. 示例文章\nURL：https://example.com\n内容：有趣的设计灵感"
             ])) as run_web_commands, \
             patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_call_actor", new=AsyncMock(return_value=saved_message["content"])) as call_actor, \
             patch.object(autonomy, "build_link_preview_attachments", new=AsyncMock(return_value=[preview])) as build_previews, \
             patch.object(autonomy, "_save_private_message", new=AsyncMock(return_value=saved_message)) as save_private, \
             patch.object(autonomy, "append_idle_event", new=fake_append):
            result = await autonomy._run_web_roam("aion")

        ask_actor.assert_awaited_once()
        run_web_commands.assert_awaited_once_with(["AI 桌面宠物设计"], [])
        call_actor.assert_awaited_once()
        build_previews.assert_awaited_once_with(saved_message["content"])
        save_private.assert_awaited_once_with("aion", saved_message["content"], [preview])
        self.assertEqual(result["message"], saved_message)
        self.assertEqual(events[0][0][1], "web_roam")
        self.assertIn("AI 桌面宠物设计", events[0][0][2])
        self.assertEqual(events[0][1]["result_type"], "message")
        self.assertEqual(events[0][1]["result_id"], "msg_web")

    async def test_web_roam_uses_search_result_source_when_reply_omits_url(self):
        reply = "我找到了一座很有意思的魔法博物馆，里面收藏了许多古老护身符。"
        web_context = "【联网搜索结果】\n1. 魔法博物馆\nURL：https://example.com/museum\n内容：馆藏介绍"
        preview = {"type": "link_preview", "url": "https://example.com/museum", "title": "魔法博物馆"}

        async def fake_previews(text, *_args, **_kwargs):
            return [] if text == reply else [preview]

        with patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={
            "search_command": "[WEB_SEARCH:魔法博物馆]", "reason": "想逛逛",
        })), patch.object(autonomy, "_is_idle_web_roam_available", return_value=True), \
             patch.object(autonomy, "run_web_commands", new=AsyncMock(return_value=[web_context])), \
             patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_call_actor", new=AsyncMock(return_value=reply)), \
             patch.object(autonomy, "build_link_preview_attachments", new=fake_previews), \
             patch.object(autonomy, "_save_private_message", new=AsyncMock(return_value={"id": "msg_web"})) as save_private, \
             patch.object(autonomy, "append_idle_event", new=AsyncMock(return_value={"id": "idle_web"})):
            await autonomy._run_web_roam("connor")

        save_private.assert_awaited_once_with("connor", reply, [preview])

    async def test_aion_private_idle_message_saves_link_preview_attachments(self):
        db = _RecordingDb()
        preview = {"type": "link_preview", "url": "https://example.com", "title": "Example"}

        with patch.object(autonomy, "_latest_conversation", new=AsyncMock(return_value=("conv_web", "unit-model"))), \
             patch.object(autonomy, "_with_link_previews", new=AsyncMock(return_value=[preview])) as with_previews, \
             patch.object(autonomy, "get_db", return_value=_DbContext(db)), \
             patch.object(autonomy.manager, "broadcast", new=AsyncMock()), \
             patch.object(autonomy.manager, "any_tts_enabled", return_value=False):
            msg = await autonomy._save_aion_private_message("看这个 https://example.com")

        with_previews.assert_awaited_once_with("看这个 https://example.com", [])
        message_inserts = [
            params for sql, params in db.statements
            if sql.startswith("INSERT INTO messages")
        ]
        self.assertEqual(len(message_inserts), 1)
        attachments = json.loads(message_inserts[0][5])
        self.assertEqual(attachments, [preview])
        self.assertEqual(msg["attachments"], [preview])


class IdleAutonomyPrivateChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = _RecordingDb()
        self.get_db_patch = patch.object(
            autonomy,
            "get_db",
            return_value=_DbContext(self.db),
        )
        self.get_db_patch.start()

    def tearDown(self):
        self.get_db_patch.stop()

    async def test_action_selection_returns_complete_chat_text_without_clipping(self):
        long_message = "自然表达。" * 180
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "private_chat", "reason": "想她了", "message": long_message}

        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={
            "actions": {"private_chat": True},
        })), patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("connor")

        self.assertEqual("private_chat", selected["action"])
        self.assertEqual(long_message, selected["message"])
        self.assertIn("实际发送的正式聊天正文", prompts[0])
        self.assertIn("不要刻意压短", prompts[0])

    async def test_private_chat_uses_the_natural_message_from_action_selection(self):
        selected = {"message": "第一段。\n\n第二段。\n[心里嘀咕：还有一点没说出口。]"}
        with patch.object(autonomy, "_ask_actor_json", new=AsyncMock()) as ask_actor, \
             patch.object(autonomy, "_save_private_message", new=AsyncMock(return_value={"id": "msg_1"})) as save:
            await autonomy._run_private_chat("connor", selected)

        ask_actor.assert_not_awaited()
        save.assert_awaited_once_with("connor", "第一段。\n\n第二段。\n[心里嘀咕：还有一点没说出口。]")
        self.assertEqual(
            1,
            sum(sql.startswith("INSERT INTO idle_events") for sql, _params in self.db.statements),
        )

    async def test_private_chat_records_only_the_objective_action(self):
        saved_message = {"id": "msg_1", "content": "刚刚想到你。"}
        with patch.object(
            autonomy,
            "_save_private_message",
            new=AsyncMock(return_value=saved_message),
        ), patch.object(
            autonomy,
            "append_idle_event",
            new=AsyncMock(return_value={"id": "idle_1"}),
        ) as append_event, patch.object(
            autonomy,
            "_actor_label",
            return_value="Configured Second",
        ):
            result = await autonomy._run_private_chat("connor", {"message": saved_message["content"]})

        self.assertEqual({"id": "idle_1"}, result["event"])
        self.assertEqual("private_chat", append_event.await_args.args[1])
        self.assertEqual("Configured Second说了一句话", append_event.await_args.args[2])
        self.assertEqual("", append_event.await_args.args[3])

    async def test_private_chat_without_a_saved_message_does_not_record_an_action(self):
        with patch.object(
            autonomy,
            "_save_private_message",
            new=AsyncMock(return_value=None),
        ), patch.object(
            autonomy,
            "append_idle_event",
            new=AsyncMock(),
        ) as append_event:
            with self.assertRaises(RuntimeError):
                await autonomy._run_private_chat("connor", {"message": ""})

        append_event.assert_not_awaited()


class IdleAutonomyFriendVisitSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_visit_action_only_available_with_eligible_owned_friend(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "friend_visit", "reason": "想去看看朋友"}

        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        })), \
             patch.object(autonomy, "eligible_lounge_friends", return_value=[], create=True), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("aion", manual=False)

        self.assertEqual(selected["action"], "rest")
        self.assertNotIn("friend_visit", prompts[0])
        self.assertNotIn("拜访一位 AI 好友", prompts[0])

    async def test_friend_visit_action_is_available_with_eligible_owned_friend(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "friend_visit", "reason": "想去看看朋友"}

        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        })), \
             patch.object(autonomy, "eligible_lounge_friends", return_value=[object()], create=True), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("aion", manual=False)

        self.assertEqual(selected["action"], "friend_visit")
        self.assertIn("friend_visit", prompts[0])
        self.assertIn("拜访一位 AI 好友", prompts[0])

    async def test_manual_run_once_still_hides_friend_visit_without_autonomous_permission(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "friend_visit", "reason": "用户正在测试"}

        with patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        })), \
             patch.object(autonomy, "eligible_lounge_friends", return_value=[], create=True), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask):
            selected = await autonomy._select_action("aion", manual=True)

        self.assertEqual(selected["action"], "rest")
        self.assertNotIn("friend_visit", prompts[0])


class IdleAutonomyRoleChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_chat_uses_natural_message_from_action_selection_without_extra_call(self):
        selected = {
            "action": "role_chat",
            "reason": "想看看 Aion 对当前情况怎么判断",
            "message": "第一段，把此刻真正想说的事讲清楚。\n\n"
                       "第二段，自然接着聊。\n[心里嘀咕：这才像平时说话。]",
        }
        events = []

        async def fake_append(*args, **kwargs):
            events.append((args, kwargs))
            return {"id": f"event_{len(events)}"}

        with patch.object(autonomy, "_select_action", new=AsyncMock(return_value=selected)), \
             patch.object(autonomy, "_latest_group_room_id", new=AsyncMock(return_value="room_role")), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock()) as ask_actor, \
             patch.object(autonomy, "append_idle_event", new=fake_append), \
             patch("routes.chatroom._save_msg", new=AsyncMock()) as save_msg, \
             patch("routes.chatroom._load_room_and_messages", new=AsyncMock(return_value=({"context_minutes": 30}, []))), \
             patch("routes.chatroom._reply_connor", new=AsyncMock()) as reply_connor, \
             patch("routes.chatroom._reply_aion", new=AsyncMock()) as reply_aion:
            result = await autonomy._run_actor_once("aion", manual=True)

        self.assertEqual(result["action"], "role_chat")
        ask_actor.assert_not_awaited()
        save_msg.assert_awaited_once_with(
            "room_role",
            "aion",
            "第一段，把此刻真正想说的事讲清楚。\n\n第二段，自然接着聊。\n[心里嘀咕：这才像平时说话。]",
        )
        reply_connor.assert_awaited_once()
        reply_aion.assert_not_awaited()
        self.assertEqual(events[0][0][1], "select")
        self.assertEqual(events[1][0][1], "role_chat")


if __name__ == "__main__":
    unittest.main()
