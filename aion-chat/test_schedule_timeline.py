import importlib
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
from fastapi import FastAPI

import routine_schedule
import schedule
from routes import routines, schedule as schedule_routes


class SharedTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "calendar.db"

        @asynccontextmanager
        async def connection():
            async with aiosqlite.connect(self.path) as db:
                yield db

        self.connection = connection
        self.patches = [patch.object(module, "get_db", connection) for module in (routine_schedule, schedule, schedule_routes)]
        self.patches += [patch.object(schedule.manager, "broadcast", AsyncMock())]
        for item in self.patches:
            item.start()
        self.addAsyncCleanup(self.cleanup)
        await routine_schedule.init_routine_storage()
        async with connection() as db:
            await db.execute("""CREATE TABLE schedules (
                id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT,
                created_at REAL, status TEXT, origin TEXT, origin_room_id TEXT, ended_at REAL
            )""")
            await db.commit()
        app = FastAPI()
        app.include_router(routines.router)
        app.include_router(schedule_routes.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def cleanup(self):
        if hasattr(self, "client"):
            await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def rows(self):
        async with self.connection() as db:
            db.row_factory = aiosqlite.Row
            return [dict(row) for row in await (await db.execute("SELECT * FROM schedules ORDER BY trigger_at")).fetchall()]

    async def test_old_reminder_and_routine_share_the_timeline_with_distinct_status(self):
        await routine_schedule.save_item(datetime(2026, 11, 9).date(), "weekly", {"content": "下午运动", "start": "15:30", "end": "17:00"})
        await schedule._add_schedule("alarm", "2026-11-09 13:50", "访谈提醒", "aion")
        response = await self.client.get("/api/routines/timeline", params={"date":"2026-11-09"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        slot = next(row for row in data["items"] if row["content"] == "下午运动")
        interview = next(row for row in data["items"] if row["content"] == "访谈提醒")
        self.assertEqual(slot["kind"], "routine")
        self.assertEqual(slot["reminders"], [])
        self.assertEqual(interview["kind"], "schedule")
        self.assertEqual(interview["reminders"][0]["type"], "alarm")
        self.assertTrue(interview["origin_name"])
        self.assertFalse(interview["event_time_known"])

    async def test_shared_creators_do_not_duplicate_same_pending_alarm(self):
        await schedule._add_schedule("alarm", "2026-11-09 13:50", "访谈", "aion")
        await schedule._add_schedule("alarm", "2026-11-09T13:50", " 访谈 ", "connor")
        self.assertEqual(len(await self.rows()), 1)
        await schedule._add_schedule("alarm", "2026-11-09 13:40", "访谈", "connor")
        self.assertEqual(len(await self.rows()), 2)
        item = (await self.rows())[0]
        await schedule._del_schedule(item["id"])
        await schedule._add_schedule("alarm", item["trigger_at"], "访谈", "aion")
        self.assertEqual(len([row for row in await self.rows() if row["status"] == "active"]), 2)

    async def test_delete_and_recreate_keeps_the_replacement_active(self):
        original = await schedule._add_schedule("monitor", "2099-11-09 13:50", "访谈", "aion")
        text = await schedule.process_schedule_commands(
            f"改好了。[SCHEDULE_DEL:{original['id']}][Monitor:2099-11-09T13:50|访谈！]",origin="connor")
        self.assertEqual(text,"改好了。")
        pending = [row for row in await self.rows() if row["status"]=="active"]
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]["content"],"访谈！")
        self.assertNotEqual(pending[0]["id"],original["id"])

    async def test_plan_can_gain_a_reminder_and_linked_retries_reuse_it(self):
        created = await routine_schedule.save_item(datetime(2026, 11, 9).date(), "weekly", {"content": "下午运动", "start": "15:30", "end": "17:00"})
        response = await self.client.put(f"/api/routines/items/{created['id']}", json={
            "date":"2026-11-09", "scope":"day", "content":"下午运动", "start":"15:30", "end":"17:00",
            "remind":True, "reminder_at":"2026-11-09T15:20",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(await self.rows()), 1)
        await schedule._add_schedule("alarm", "2026-11-09 15:20", "运动别忘了", "connor", event_key=f"routine:2026-11-09:{created['id']}")
        self.assertEqual(len(await self.rows()), 1)
        response = await self.client.get("/api/routines/timeline", params={"date":"2026-11-09"})
        slot_rows = [row for row in response.json()["items"] if row["id"] == created["id"]]
        self.assertEqual(len(slot_rows), 1)
        self.assertEqual(len(slot_rows[0]["reminders"]), 1)

    async def test_sharing_switch_only_controls_added_default_routines(self):
        saturday = datetime(2026, 11, 7).date()
        await routine_schedule.save_item(saturday, "weekly", {"content": "上午自习", "start": "10:30", "end": "13:00"})
        await routine_schedule.save_item(saturday, "weekly", {"content": "午间休息", "start": "13:00", "end": "14:30"})
        await routine_schedule.save_item(saturday, "weekly", {"content": "睡前阅读", "start": "22:00", "end": "23:30"})
        await schedule._add_schedule("alarm", "2026-11-09 13:50", "访谈", "aion")
        calendar = importlib.import_module("schedule_timeline")
        now = datetime(2026, 11, 7, 11, 0)
        brief = await calendar.build_routine_prompt(now=now)
        self.assertEqual(brief, "以下是计划，实际聊天信息优先。\n今日：10:30—13:00 上午自习｜默认安排\n下一项：13:00—14:30 午间休息｜默认安排")
        self.assertNotIn("访谈", brief)
        self.assertNotIn("睡前阅读", brief)
        result = await self.client.put("/api/routines/sharing", json={"enabled":False})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(await calendar.build_routine_prompt(now=now), "")
        self.assertEqual((await self.rows())[0]["status"], "active")

    async def test_structured_alarm_metadata_groups_one_event_and_updates_in_place(self):
        with patch.object(schedule, "_is_schedule_time_stale", return_value=False):
            text = await schedule.process_schedule_commands(
                "会提醒你的。[ALARM:2026-11-09T13:50|访谈|event_at=2026-11-09T14:00]", origin="aion")
            self.assertEqual(text, "会提醒你的。")
            item = (await self.rows())[0]
            self.assertEqual(item["content"], "访谈")
            await schedule.process_schedule_commands(
                f"[ALARM:2026-11-09T13:40|访谈|event_at=2026-11-09T14:00|id={item['id']}]", origin="connor")
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger_at"], "2026-11-09 13:40")
        timeline = (await self.client.get("/api/routines/timeline", params={"date":"2026-11-09"})).json()
        interview = next(row for row in timeline["items"] if row["content"] == "访谈")
        self.assertEqual(interview["start"], "14:00")
        self.assertTrue(interview["event_time_known"])

    async def test_triggered_reminder_keeps_the_event_but_is_no_longer_pending(self):
        await schedule._add_schedule("alarm", "2026-11-09 13:50", "访谈", "aion", event_at="2026-11-09 14:00")
        async with self.connection() as db:
            await db.execute("UPDATE schedules SET status='triggered', ended_at=1")
            await db.commit()
        response = await self.client.get("/api/routines/timeline", params={"date":"2026-11-09"})
        interview = next(row for row in response.json()["items"] if row["content"] == "访谈")
        self.assertEqual(interview["start"], "14:00")
        self.assertEqual(interview["reminders"], [])
        self.assertEqual(interview["reminder_state"], "triggered")

    async def test_deleting_a_date_plan_cancels_only_its_linked_reminder(self):
        created = await routine_schedule.save_item(datetime(2026, 11, 9).date(), "weekly", {"content": "下午运动", "start": "15:30", "end": "17:00"})
        await self.client.put(f"/api/routines/items/{created['id']}", json={"date":"2026-11-09","scope":"day","content":"下午运动","start":"15:30","end":"17:00","remind":True,"reminder_at":"2026-11-09T15:20"})
        await schedule._add_schedule("alarm", "2026-11-09 18:00", "独立访谈提醒", "aion")
        await self.client.delete(f"/api/routines/items/{created['id']}", params={"date":"2026-11-09","scope":"day"})
        rows = await self.rows()
        self.assertEqual(next(row["status"] for row in rows if row["content"] == "下午运动"), "cancelled")
        self.assertEqual(next(row["status"] for row in rows if row["content"] == "独立访谈提醒"), "active")

    async def test_calendar_context_stays_after_capabilities_and_before_device_context(self):
        import context_builder as context
        monday = await schedule._add_schedule("alarm", "2026-11-09 14:50", "周一访谈", "connor")
        tonight = await schedule._add_schedule("monitor", "2026-11-07 22:30", "晚间监督", "aion")
        with patch.object(context,"get_db",self.connection), \
             patch.object(context,"build_capability_prompt_items",AsyncMock(return_value=["CAPABILITY-SENTINEL"])), \
             patch.object(context,"is_capability_enabled",side_effect=lambda name:name=="schedule"), \
             patch.object(context,"get_device_context_for_prompt",return_value="DEVICE-SENTINEL"), \
             patch.object(context,"build_cli_file_storage_text",return_value=""), \
             patch("external_memories.recent_context",AsyncMock(return_value="")), \
             patch("external_activity.recent_context",AsyncMock(return_value="")):
            result = await context.build_ability_block("Configured User",excluded_capabilities={"post_sentinel"},who="aion")
            self.assertIn(monday["id"], result)
            self.assertIn(tonight["id"], result)
            self.assertLess(result.index(monday["id"]),result.index("以下是计划，实际聊天信息优先。"))
            await self.client.put("/api/routines/sharing",json={"enabled":False})
            without_routines = await context.build_ability_block("Configured User",excluded_capabilities={"post_sentinel"},who="connor")
            self.assertIn(monday["id"], without_routines)
            self.assertIn(tonight["id"], without_routines)
            self.assertNotIn("以下是计划", without_routines)
        # The original command still cancels another companion's entry by its visible id.
        await schedule.process_schedule_commands(f"[SCHEDULE_DEL:{monday['id']}]",origin="aion")
        self.assertEqual(next(row["status"] for row in await self.rows() if row["id"]==monday["id"]),"cancelled")
        self.assertEqual(next(row["status"] for row in await self.rows() if row["id"]==tonight["id"]),"active")
        self.assertLess(result.index("CAPABILITY-SENTINEL"),result.index("【当前日程列表】"))
        self.assertLess(result.index("【当前日程列表】"),result.index("DEVICE-SENTINEL"))

    async def test_routine_overnight_current(self):
        calendar = importlib.import_module("schedule_timeline")
        await routine_schedule.save_item(datetime(2026,11,7).date(),"day",{"content":"跨夜休息","start":"23:00","end":"01:00"})
        answer = await calendar.build_routine_prompt(now=datetime(2026,11,8,0,30))
        self.assertIn("当前：2026-11-07 23:00—01:00 跨夜休息",answer)

    async def test_cancelled_reference_record_leaves_the_timeline(self):
        record = await schedule._add_schedule("reminder", "2026-11-09 14:00", "临时访谈记录", "aion", event_at="2026-11-09 14:00")
        await self.client.delete("/api/schedules/" + record["id"])
        timeline = (await self.client.get("/api/routines/timeline", params={"date":"2026-11-09"})).json()
        self.assertNotIn("临时访谈记录", [row["content"] for row in timeline["items"]])

    async def test_legacy_supervision_is_not_mixed_into_default_routines(self):
        await routine_schedule.save_item(datetime(2026, 11, 7).date(), "weekly", {"content": "晚间整理", "start": "21:30", "end": "22:00"})
        for stype, when, content, status in (
            ("monitor", "09:40", "上午监督任务", "triggered"),
            ("monitor", "11:30", "取消的监督任务", "cancelled"),
            ("reminder", "16:00", "过期的监督记录", "active"),
            ("monitor", "20:00", "晚间监督任务", "active"),
        ):
            record = await schedule._add_schedule(stype, "2026-11-07 " + when, content, "aion")
            async with self.connection() as db:
                await db.execute("UPDATE schedules SET status=? WHERE id=?", (status, record["id"]))
                await db.commit()
        calendar = importlib.import_module("schedule_timeline")
        now = datetime(2026, 11, 7, 19, 45)
        result = await calendar.build_routine_prompt(now=now)
        self.assertIn("下一项：21:30—22:00 晚间整理｜默认安排", result)
        self.assertNotIn("ref=", result)
        for title in ("上午监督任务", "取消的监督任务", "过期的监督记录", "晚间监督任务", "按需查询"):
            self.assertNotIn(title, result)


if __name__ == "__main__":
    unittest.main()
