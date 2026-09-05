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


if __name__ == "__main__":
    unittest.main()
