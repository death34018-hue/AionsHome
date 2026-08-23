import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import aiosqlite

import proactive_companionship as pc
import schedule as schedule_module


SCHEDULE_SCHEMA = """
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    ended_at REAL,
    origin TEXT DEFAULT 'aion',
    origin_room_id TEXT DEFAULT ''
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conv_id TEXT,
    role TEXT,
    content TEXT,
    created_at REAL
);
CREATE TABLE chatroom_messages (
    id TEXT PRIMARY KEY,
    room_id TEXT,
    sender TEXT,
    content TEXT,
    created_at REAL
);
"""


class ProactiveCompanionshipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEDULE_SCHEMA)
            await db.commit()
        self.old_db_path = pc.DB_PATH
        self.old_schedule_db_path = schedule_module.DB_PATH
        self.old_settings = dict(pc.SETTINGS)
        pc.DB_PATH = self.db_path
        schedule_module.DB_PATH = self.db_path
        pc.SETTINGS["proactive_companionship_aion_enabled"] = True
        pc.SETTINGS["proactive_companionship_connor_enabled"] = True

    async def asyncTearDown(self):
        pc.DB_PATH = self.old_db_path
        schedule_module.DB_PATH = self.old_schedule_db_path
        pc.SETTINGS.clear()
        pc.SETTINGS.update(self.old_settings)
        self.tmp.cleanup()

    def test_extracts_last_valid_minute_or_none_and_strips_commands(self):
        clean, decision = pc.extract_next_chat_decision(
            "先歇一会儿 [NEXT_CHAT:5] 后面改成 [NEXT_CHAT: 7 ]"
        )
        self.assertEqual(clean, "先歇一会儿  后面改成")
        self.assertEqual(decision, 7)

        clean, decision = pc.extract_next_chat_decision("好呀[NEXT_CHAT:NONE]")
        self.assertEqual(clean, "好呀")
        self.assertIsNone(decision)

        clean, decision = pc.extract_next_chat_decision("越界不生效[NEXT_CHAT:61]")
        self.assertEqual(clean, "越界不生效")
        self.assertIs(decision, pc.NO_DECISION)

    async def _seed_timer(self, actor: str, sid: str, trigger_at: str = "2099-01-01 00:00:00"):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO schedules (id,type,trigger_at,content,created_at,status,origin,origin_room_id) "
                "VALUES (?,?,?,?,?,'active',?,'')",
                (sid, pc.PROACTIVE_TYPE, trigger_at, "", 1.0, actor),
            )
            await db.commit()

    async def _active_timers(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, origin, trigger_at FROM schedules "
                "WHERE type=? AND status='active' ORDER BY origin",
                (pc.PROACTIVE_TYPE,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def test_user_message_clears_both_role_timers(self):
        await self._seed_timer("aion", "old-a")
        await self._seed_timer("connor", "old-c")

        _, changed = await pc.process_visible_message_event({
            "type": "msg_created",
            "data": {"id": "u1", "role": "user", "content": "在吗", "created_at": 1000.0},
        })

        self.assertTrue(changed)
        self.assertEqual(await self._active_timers(), [])

    async def test_ai_message_replaces_only_its_own_timer_after_message_time(self):
        await self._seed_timer("aion", "old-a")
        await self._seed_timer("connor", "old-c")
        created_at = 1_800_000_000.0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages (id,conv_id,role,content,created_at) VALUES (?,?,?,?,?)",
                ("a1", "conv", "assistant", "我过会儿再来[NEXT_CHAT:7]", created_at),
            )
            await db.commit()

        event, changed = await pc.process_visible_message_event({
            "type": "msg_created",
            "data": {
                "id": "a1",
                "conv_id": "conv",
                "role": "assistant",
                "content": "我过会儿再来[NEXT_CHAT:7]",
                "created_at": created_at,
            },
        })

        self.assertTrue(changed)
        self.assertEqual(event["data"]["content"], "我过会儿再来")
        timers = await self._active_timers()
        self.assertEqual([row["origin"] for row in timers], ["aion", "connor"])
        aion_timer = next(row for row in timers if row["origin"] == "aion")
        expected = datetime.fromtimestamp(created_at + 7 * 60).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(aion_timer["trigger_at"], expected)
        self.assertEqual(next(row for row in timers if row["origin"] == "connor")["id"], "old-c")

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT content FROM messages WHERE id='a1'")
            self.assertEqual((await cur.fetchone())[0], "我过会儿再来")

    async def test_claim_removes_timer_once(self):
        await self._seed_timer("aion", "due-a", "2000-01-01 00:00:00")

        self.assertTrue(await pc.claim_proactive_timer("due-a"))
        self.assertFalse(await pc.claim_proactive_timer("due-a"))

    async def test_schedule_tick_dispatches_due_proactive_timer_through_monitor_capture(self):
        await self._seed_timer("aion", "due-a", "2000-01-01 00:00:00")
        manager = schedule_module.ScheduleManager()
        fired_as_alarm = []
        fired_as_monitor = []

        async def capture_alarm(item):
            fired_as_alarm.append(item)

        async def capture_monitor(item):
            fired_as_monitor.append(item)

        manager._fire_alarm = capture_alarm
        manager._fire_monitor = capture_monitor
        await manager._tick()

        self.assertEqual(fired_as_alarm, [])
        self.assertEqual([item["id"] for item in fired_as_monitor], ["due-a"])
        self.assertEqual(fired_as_monitor[0]["type"], pc.PROACTIVE_TYPE)

    def test_shared_prompt_is_role_gated_and_does_not_expose_old_timer(self):
        text = pc.proactive_ability_text("aion")
        self.assertIn("[NEXT_CHAT:x]", text)
        self.assertIn("1～60", text)
        self.assertNotIn("当前计时", text)
        pc.SETTINGS["proactive_companionship_aion_enabled"] = False
        self.assertEqual(pc.proactive_ability_text("aion"), "")

    def test_proactive_monitor_card_is_only_created_when_snapshot_exists(self):
        self.assertEqual(
            schedule_module.proactive_monitor_card_title("角色名", {"type": "image"}),
            "角色名查看了监控",
        )
        self.assertEqual(schedule_module.proactive_monitor_card_title("角色名", None), "")

    def test_message_broadcast_and_main_context_use_shared_companion_hooks(self):
        root = Path(__file__).parent
        ws_source = (root / "ws.py").read_text(encoding="utf-8")
        capabilities_source = (root / "capabilities.py").read_text(encoding="utf-8")
        self.assertIn("process_visible_message_event", ws_source)
        self.assertIn("proactive_status_event", ws_source)
        self.assertIn("proactive_ability_text(who)", capabilities_source)


if __name__ == "__main__":
    unittest.main()
