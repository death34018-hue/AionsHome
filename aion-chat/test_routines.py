"""The routine API must preserve independent weekdays and date-only changes."""

import importlib
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
from fastapi import FastAPI


class RoutineApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = importlib.import_module("routine_schedule")
        self.routes = importlib.import_module("routes.routines")
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "routines.db"

        @asynccontextmanager
        async def temporary_db():
            async with aiosqlite.connect(self.db_path) as db:
                yield db

        self.db_patch = patch.object(self.store, "get_db", temporary_db)
        self.db_patch.start()
        self.addAsyncCleanup(self._cleanup)
        self.broadcast_patch = patch.object(self.routes.manager, "broadcast", AsyncMock())
        self.broadcast_patch.start()
        await self.store.init_routine_storage()
        app = FastAPI()
        app.include_router(self.routes.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _cleanup(self):
        if hasattr(self, "client"):
            await self.client.aclose()
        self.broadcast_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def get_day(self, day):
        response = await self.client.get("/api/routines", params={"date": day})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def write(self, method, day, content, item_id=None, scope="weekly", start="15:30", end="17:00", apply_to_date=False):
        response = await self.client.request(
            method, "/api/routines/items" + (f"/{item_id}" if item_id else ""),
            json={"date": day, "scope": scope, "content": content, "start": start, "end": end, "apply_to_date": apply_to_date},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_weekday_crud_repeats_without_changing_other_weekdays(self):
        monday = await self.get_day("2026-11-02")
        tuesday = await self.get_day("2026-11-03")
        self.assertIn("健身 · 练胸", [row["content"] for row in monday["items"]])
        self.assertIn("健身 · 练背", [row["content"] for row in tuesday["items"]])
        created = await self.write("POST", "2026-11-02", "  晚间阅读  ", start="20:00", end="20:30")
        item_id = created["id"]
        self.assertEqual(created["content"], "晚间阅读")
        await self.write("PUT", "2026-11-02", "读小说", item_id, start="20:10", end="20:40")
        await self.store.init_routine_storage()
        next_monday = await self.get_day("2026-11-09")
        self.assertEqual(next(row["content"] for row in next_monday["items"] if row["id"] == item_id), "读小说")
        self.assertNotIn(item_id, [row["id"] for row in (await self.get_day("2026-11-03"))["items"]])
        deleted = await self.client.delete(f"/api/routines/items/{item_id}", params={"date": "2026-11-02", "scope": "weekly"})
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(item_id, [row["id"] for row in (await self.get_day("2026-11-09"))["items"]])

    async def test_date_overrides_merge_with_weekly_changes_and_can_be_reset(self):
        monday = await self.get_day("2026-11-02")
        gym = next(row for row in monday["items"] if row["start"] == "15:30")
        dinner = next(row for row in monday["items"] if row["start"] == "18:30")
        await self.write("PUT", "2026-11-02", "今天休息", gym["id"], scope="day")
        extra = await self.write("POST", "2026-11-02", "临时出门", scope="day", start="19:30", end="20:00")
        await self.write("PUT", "2026-11-02", "外出吃晚餐", dinner["id"], start="18:30", end="19:30")
        today = await self.get_day("2026-11-02")
        self.assertTrue(today["has_overrides"])
        contents = [row["content"] for row in today["items"]]
        self.assertIn("今天休息", contents)
        self.assertIn("外出吃晚餐", contents)
        self.assertIn("临时出门", contents)
        next_week = await self.get_day("2026-11-09")
        self.assertIn("健身 · 练胸", [row["content"] for row in next_week["items"]])
        self.assertNotIn(extra["id"], [row["id"] for row in next_week["items"]])
        await self.client.delete(f"/api/routines/items/{gym['id']}", params={"date": "2026-11-02", "scope": "day"})
        self.assertNotIn(gym["id"], [row["id"] for row in (await self.get_day("2026-11-02"))["items"]])
        reset = await self.client.delete("/api/routines/overrides/2026-11-02")
        self.assertEqual(reset.status_code, 200)
        restored = await self.get_day("2026-11-02")
        self.assertFalse(restored["has_overrides"])
        self.assertIn("健身 · 练胸", [row["content"] for row in restored["items"]])

    async def test_copy_does_not_link_weekdays_and_empty_plan_survives_restart(self):
        copied = await self.client.post("/api/routines/copy", json={"source_weekday": 0, "target_weekdays": [1]})
        self.assertEqual(copied.status_code, 200, copied.text)
        tuesday = await self.get_day("2026-11-03")
        gym = next(row for row in tuesday["weekly_items"] if row["start"] == "15:30")
        await self.write("PUT", "2026-11-03", "改回练背", gym["id"])
        self.assertIn("健身 · 练胸", [row["content"] for row in (await self.get_day("2026-11-02"))["items"]])
        for row in (await self.get_day("2026-11-03"))["weekly_items"]:
            response = await self.client.delete(f"/api/routines/items/{row['id']}", params={"date": "2026-11-03", "scope": "weekly"})
            self.assertEqual(response.status_code, 200)
        await self.store.init_routine_storage()
        self.assertEqual((await self.get_day("2026-11-10"))["items"], [])

    async def test_promoting_date_edit_updates_that_date_without_erasing_other_exceptions(self):
        gym = next(row for row in (await self.get_day("2026-11-02"))["items"] if row["start"] == "15:30")
        await self.write("PUT", "2026-11-02", "当天散步", gym["id"], scope="day")
        await self.write("PUT", "2026-11-09", "下周出门", gym["id"], scope="day")
        await self.write("PUT", "2026-11-02", "每周游泳", gym["id"])
        self.assertIn("当天散步", [row["content"] for row in (await self.get_day("2026-11-02"))["items"]])
        await self.write("PUT", "2026-11-02", "每周拉伸", gym["id"], apply_to_date=True)
        self.assertIn("每周拉伸", [row["content"] for row in (await self.get_day("2026-11-02"))["items"]])
        self.assertIn("下周出门", [row["content"] for row in (await self.get_day("2026-11-09"))["items"]])
        self.assertIn("每周拉伸", [row["content"] for row in (await self.get_day("2026-11-16"))["items"]])

    async def test_invalid_input_is_rejected_without_changing_the_plan(self):
        before = (await self.get_day("2026-11-02"))["items"]
        for values in ({"content": "   ", "start": "09:00"}, {"content": "坏时间", "start": "25:00"}):
            response = await self.client.post("/api/routines/items", json={"date": "2026-11-02", "scope": "weekly", **values})
            self.assertEqual(response.status_code, 422, response.text)
        response = await self.client.get("/api/routines", params={"date": "wrong-date"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual((await self.get_day("2026-11-02"))["items"], before)


if __name__ == "__main__":
    unittest.main()
