import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


class ShoppingNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.sqlite3"
        async with self.db() as db:
            await db.executescript("""
                CREATE TABLE conversations(id TEXT PRIMARY KEY,updated_at REAL);
                CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY,type TEXT,updated_at REAL);
                CREATE TABLE messages(id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL,attachments TEXT);
                CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,created_at REAL,attachments TEXT);
                CREATE TABLE sync_events(seq INTEGER PRIMARY KEY,event_type TEXT,entity_type TEXT,entity_id TEXT,payload TEXT,created_at REAL);
                INSERT INTO conversations VALUES('private-old',1),('private-latest',2);
                INSERT INTO chatroom_rooms VALUES('group-last','group',1),('connor-private','connor_1v1',2);
            """)
        self.items = [{"item_id": str(123450+i), "title": "商品"+str(i),
                       "image": "https://img.alicdn.com/test.jpg", "url": "https://item.taobao.com/item.htm?id="+str(123450+i)} for i in range(4)]

    @asynccontextmanager
    async def db(self):
        async with aiosqlite.connect(self.path) as db:
            yield db

    async def test_last_active_routes_and_durable_single_card(self):
        from taobao_notifications import notify_shopping_trip
        from ws import manager
        for actor, active, table, expected in [
            ("aion", "private", "messages", "private-latest"),
            ("aion", "chatroom:group-last", "chatroom_messages", "group-last"),
            ("connor", "group-last", "chatroom_messages", "group-last"),
        ]:
            trip_id = actor + active.replace(":", "-")
            with patch("taobao_notifications.get_db", self.db), \
                 patch("taobao_notifications.get_chatroom_names", return_value=("家人", "测试甲", "测试乙")), \
                 patch.object(manager, "get_aion_last_active", return_value=active), \
                 patch.object(manager, "get_connor_last_active", return_value=active), \
                 patch.object(manager, "broadcast", new=AsyncMock()) as send:
                message = await notify_shopping_trip(actor, trip_id, self.items)
                duplicate = await notify_shopping_trip(actor, trip_id, self.items)
            self.assertIsNone(duplicate)
            self.assertEqual(send.await_count, 1)
            self.assertEqual(message.get("conv_id", message.get("room_id")), expected)
            self.assertEqual(message.get("role", message.get("sender")), "system")
            self.assertIn("4 件", message["content"])
            self.assertIn("测试甲" if actor == "aion" else "测试乙", message["content"])
            async with self.db() as db:
                row = await (await db.execute(f"SELECT attachments FROM {table} WHERE id=?", (message["id"],))).fetchone()
                event = await (await db.execute("SELECT payload FROM sync_events WHERE entity_id=?", (message["id"],))).fetchone()
            card = json.loads(row[0])[0]
            self.assertEqual((card["type"], card["count"], len(card["products"])), ("taobao_trip", 4, 3))
            self.assertEqual(card["trip_id"], trip_id)
            self.assertEqual(json.loads(event[0])["id"], message["id"])

    async def test_no_products_no_chat_message(self):
        from taobao_notifications import notify_shopping_trip
        with patch("taobao_notifications.get_db", self.db):
            self.assertIsNone(await notify_shopping_trip("connor", "empty", []))
        async with self.db() as db:
            self.assertEqual((await (await db.execute("SELECT COUNT(*) FROM chatroom_messages")).fetchone())[0], 0)

    async def test_private_card_follows_last_user_message_not_background_updates(self):
        from taobao_notifications import notify_shopping_trip
        from ws import manager
        async with self.db() as db:
            await db.execute("INSERT INTO messages VALUES('last-user','private-old','user','你好',3,'[]')")
            await db.execute("UPDATE conversations SET updated_at=999 WHERE id='private-latest'")
            await db.commit()
        with patch("taobao_notifications.get_db", self.db), \
             patch.object(manager, "get_aion_last_active", return_value="private"), \
             patch.object(manager, "broadcast", new=AsyncMock()):
            message = await notify_shopping_trip("aion", "last-user-trip", self.items)
        self.assertEqual(message["conv_id"], "private-old")

    async def test_card_loads_outing_essentials_into_private_and_group_context(self):
        from context_builder import fetch_merged_timeline, render_merged_timeline
        from taobao_notifications import notify_shopping_trip
        from ws import manager

        selected = [{**p, "price": "2.82", "reflection": "这次喜欢它的造型",
                     "recipient": "家人", "purpose": "夹住零食袋", "shop": "不必带入的店铺"}
                    for p in self.items]
        async with aiosqlite.connect(self.path.with_name("taobao.sqlite3")) as db:
            await db.execute("CREATE TABLE shopping_trips(id TEXT PRIMARY KEY,selected TEXT)")
            await db.execute("INSERT INTO shopping_trips VALUES('context-trip',?)",
                             (json.dumps(selected, ensure_ascii=False),))
            await db.commit()

        for actor, active in (("aion", "private"), ("aion", "chatroom:group-last"),
                              ("connor", "group-last"), ("connor", "connor-private")):
            with self.subTest(actor=actor, active=active), \
                 patch("taobao_notifications.get_db", self.db), \
                 patch("context_builder.get_db", self.db), \
                 patch("taobao_context.DB_PATH", self.path), \
                 patch("context_builder._timeline_display_names", return_value=("家人", "测试甲", "测试乙")), \
                 patch.object(manager, "get_aion_last_active", return_value=active), \
                 patch.object(manager, "get_connor_last_active", return_value=active), \
                 patch.object(manager, "broadcast", new=AsyncMock()):
                async with self.db() as db:
                    for table in ("messages", "chatroom_messages", "sync_events"):
                        await db.execute(f"DELETE FROM {table}")
                    await db.commit()
                # Original three-product card has no new context fields: old cards work too.
                notice = await notify_shopping_trip(actor, "context-trip", self.items)
                history = render_merged_timeline(await fetch_merged_timeline(actor, 1), actor)
            entries = [m for m in history if "[逛淘宝记录]" in m["content"]]
            self.assertEqual(len(entries), 1)
            text = entries[0]["content"]
            for value in ("历史消息 - 系统事件", "商品3", "¥2.82", "这次喜欢它的造型",
                          "想给谁：家人", "用途：夹住零食袋", "仅收藏，未购买", "发现时快照",
                          "测试甲" if actor == "aion" else "测试乙"):
                self.assertIn(value, text)
            self.assertNotIn("不必带入的店铺", text)
            self.assertNotIn("https://", text)
            self.assertNotIn("选品感想", notice["content"])
            self.assertNotIn("attachments", entries[0])

    async def test_missing_outing_uses_card_without_inventing_essentials(self):
        from context_builder import fetch_merged_timeline, render_merged_timeline
        from taobao_notifications import notify_shopping_trip
        from ws import manager
        with patch("taobao_notifications.get_db", self.db), \
             patch("context_builder.get_db", self.db), \
             patch("taobao_context.DB_PATH", self.path), \
             patch.object(manager, "get_aion_last_active", return_value="private"), \
             patch.object(manager, "broadcast", new=AsyncMock()):
            await notify_shopping_trip("aion", "missing-trip", self.items)
            history = render_merged_timeline(await fetch_merged_timeline("aion", 1), "aion")
        text = history[-1]["content"]
        self.assertIn("商品0", text)
        self.assertIn("完整小记已不可用", text)
        self.assertIn("当时价格：未提供", text)
        self.assertNotIn("选品感想：", text)
        self.assertFalse(self.path.with_name("taobao.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
