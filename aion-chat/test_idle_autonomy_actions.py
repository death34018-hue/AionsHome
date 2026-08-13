import sys
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonomy


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
    def test_memory_browse_is_not_an_idle_autonomy_choice(self):
        stale_settings = {
            "idle_autonomy_actions": {
                "memory_browse": True,
                "home_dynamics": True,
            }
        }

        with patch.object(autonomy, "SETTINGS", stale_settings):
            cfg = autonomy.get_idle_config()

        self.assertNotIn("memory_browse", autonomy.ACTION_DEFS)
        self.assertNotIn("memory_browse", cfg["actions"])
        self.assertIn("home_dynamics", cfg["actions"])
        self.assertIn("web_roam", autonomy.ACTION_DEFS)
        self.assertIn("web_roam", cfg["actions"])

    def test_friend_visit_is_an_idle_autonomy_choice(self):
        self.assertEqual(autonomy.ACTION_DEFS["friend_visit"], "拜访一位 AI 好友")

    def test_saving_idle_config_drops_stale_memory_browse_setting(self):
        stale_settings = {
            "idle_autonomy_actions": {
                "memory_browse": True,
                "home_dynamics": True,
            }
        }

        with patch.object(autonomy, "SETTINGS", stale_settings), \
             patch.object(autonomy, "save_settings") as save_settings:
            cfg = autonomy.save_idle_config(actions={"home_dynamics": False})

        self.assertNotIn("memory_browse", cfg["actions"])
        self.assertNotIn("memory_browse", stale_settings["idle_autonomy_actions"])
        save_settings.assert_called_once_with(stale_settings)


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
             patch.object(autonomy, "_ask_actor_json", new=fake_ask), \
             patch.object(autonomy.random, "choice", return_value="home_dynamics"):
            selected = await autonomy._select_action("aion")

        self.assertEqual(selected["action"], "home_dynamics")
        self.assertTrue(prompts)
        self.assertNotIn("web_roam", prompts[0])
        self.assertNotIn("上网冲浪", prompts[0])

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
             patch.object(autonomy, "_save_private_message", new=AsyncMock(return_value=saved_message)) as save_private, \
             patch.object(autonomy, "append_idle_event", new=fake_append):
            result = await autonomy._run_web_roam("aion")

        ask_actor.assert_awaited_once()
        run_web_commands.assert_awaited_once_with(["AI 桌面宠物设计"], [])
        call_actor.assert_awaited_once()
        save_private.assert_awaited_once_with("aion", saved_message["content"])
        self.assertEqual(result["message"], saved_message)
        self.assertEqual(events[0][0][1], "web_roam")
        self.assertIn("AI 桌面宠物设计", events[0][0][2])
        self.assertEqual(events[0][1]["result_type"], "message")
        self.assertEqual(events[0][1]["result_id"], "msg_web")

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


class IdleAutonomyFriendVisitSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_visit_action_only_available_with_eligible_owned_friend(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "friend_visit", "reason": "想去看看朋友"}

        with patch.object(autonomy, "get_idle_config", return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        }), \
             patch.object(autonomy, "eligible_lounge_friends", return_value=[], create=True), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask), \
             patch.object(autonomy.random, "choice", return_value="home_dynamics"):
            selected = await autonomy._select_action("aion", manual=False)

        self.assertEqual(selected["action"], "home_dynamics")
        self.assertNotIn("friend_visit", prompts[0])
        self.assertNotIn("拜访一位 AI 好友", prompts[0])

    async def test_friend_visit_action_is_available_with_eligible_owned_friend(self):
        prompts = []

        async def fake_ask(_actor, instruction, **_kwargs):
            prompts.append(instruction)
            return {"action": "friend_visit", "reason": "想去看看朋友"}

        with patch.object(autonomy, "get_idle_config", return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        }), \
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

        with patch.object(autonomy, "get_idle_config", return_value={
            "actions": {"friend_visit": True, "home_dynamics": True},
        }), \
             patch.object(autonomy, "eligible_lounge_friends", return_value=[], create=True), \
             patch.object(autonomy, "_ask_actor_json", new=fake_ask), \
             patch.object(autonomy.random, "choice", return_value="home_dynamics"):
            selected = await autonomy._select_action("aion", manual=True)

        self.assertEqual(selected["action"], "home_dynamics")
        self.assertNotIn("friend_visit", prompts[0])


class IdleAutonomyRoleChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_chat_reuses_selected_message_without_extra_actor_call(self):
        selected = {
            "action": "role_chat",
            "reason": "start a short chat",
            "message": "Use this line from selection",
        }
        events = []

        async def fake_append(*args, **kwargs):
            events.append((args, kwargs))
            return {"id": f"event_{len(events)}"}

        with patch.object(autonomy, "_select_action", new=AsyncMock(return_value=selected)), \
             patch.object(autonomy, "_latest_group_room_id", new=AsyncMock(return_value="room_role")), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={"message": "Generated separately"})) as ask_actor, \
             patch.object(autonomy, "append_idle_event", new=fake_append), \
             patch("routes.chatroom._save_msg", new=AsyncMock()) as save_msg, \
             patch("routes.chatroom._load_room_and_messages", new=AsyncMock(return_value=({"context_minutes": 30}, []))), \
             patch("routes.chatroom._reply_connor", new=AsyncMock()) as reply_connor, \
             patch("routes.chatroom._reply_aion", new=AsyncMock()) as reply_aion:
            result = await autonomy._run_actor_once("aion", manual=True)

        self.assertEqual(result["action"], "role_chat")
        ask_actor.assert_not_awaited()
        save_msg.assert_awaited_once_with("room_role", "aion", "Use this line from selection")
        reply_connor.assert_awaited_once()
        reply_aion.assert_not_awaited()
        self.assertEqual(events[0][0][1], "select")
        self.assertEqual(events[1][0][1], "role_chat")


if __name__ == "__main__":
    unittest.main()
