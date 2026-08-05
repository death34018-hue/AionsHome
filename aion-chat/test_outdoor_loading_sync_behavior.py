import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_events
from routes import chatroom as chatroom_routes
from routes import memories as memory_routes
from routes import sync as sync_routes


class _DbContext:
    def __init__(self, path):
        self.path = path
        self.db = None

    async def __aenter__(self):
        self.db = await aiosqlite.connect(self.path)
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.db.rollback()
        await self.db.close()
        return False


class DurableSyncBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "sync.db")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE sync_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    entity_id TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
            """)
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        return _DbContext(self.db_path)

    async def test_persisted_event_replays_with_same_legacy_shape(self):
        event = {"type": "memory_deleted", "data": {"id": "mem-7"}}
        with patch.object(sync_events, "get_db", side_effect=self._get_db):
            seq = await sync_events.persist_sync_event(event)
        self.assertEqual(seq, 1)

        with patch.object(sync_routes, "get_db", side_effect=self._get_db):
            result = await sync_routes.sync_changes(after=0, limit=20)

        self.assertFalse(result["reset_required"])
        self.assertEqual(result["events"], [{
            "type": "memory_deleted",
            "data": {"id": "mem-7"},
            "sync_seq": 1,
            "created_at": result["events"][0]["created_at"],
        }])

    async def test_cursor_ahead_of_restored_database_requires_reset(self):
        with patch.object(sync_routes, "get_db", side_effect=self._get_db):
            result = await sync_routes.sync_changes(after=99, limit=20)
        self.assertTrue(result["reset_required"])
        self.assertEqual(result["latest_seq"], 0)


class MemoryPaginationBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "memory.db")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL,
                    source_conv TEXT, keywords TEXT, importance REAL,
                    source_start_ts REAL, source_end_ts REAL, unresolved INTEGER,
                    source_msg_id TEXT, evidence_summary TEXT, evidence_detail_level TEXT,
                    archive_state TEXT
                )
            """)
            await db.execute("CREATE TABLE messages (id TEXT, role TEXT, created_at REAL)")
            await db.execute("CREATE TABLE chatroom_messages (id TEXT, sender TEXT, created_at REAL)")
            for i in range(55):
                await db.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"m{i}", f"memory {i}", "event", float(i + 1), None, "[]", 0.5,
                     None, None, 0, None, "", "summary", "active"),
                )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        return _DbContext(self.db_path)

    async def test_first_page_is_fifty_and_cursor_loads_remaining_five(self):
        with patch.object(memory_routes, "get_db", side_effect=self._get_db):
            first = await memory_routes.list_memories(limit=50, before=None, q="")
            second = await memory_routes.list_memories(
                limit=50, before=first["next_cursor"], q=""
            )

        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse(second["has_more"])
        self.assertFalse({m["id"] for m in first["items"]} & {m["id"] for m in second["items"]})


class ChatroomSettingsBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "chatroom.db")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE chatroom_rooms (
                    id TEXT PRIMARY KEY, title TEXT, type TEXT,
                    context_minutes INTEGER, ai_chat_rounds INTEGER,
                    created_at REAL, updated_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE sync_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL, entity_type TEXT NOT NULL DEFAULT '',
                    entity_id TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
            """)
            await db.execute(
                "INSERT INTO chatroom_rooms VALUES (?,?,?,?,?,?,?)",
                ("room-1", "旧标题", "group", 30, 1, 1.0, 1.0),
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        return _DbContext(self.db_path)

    async def test_aggregate_patch_returns_canonical_room_and_config(self):
        broadcast = AsyncMock()
        body = chatroom_routes.ChatroomSettingsUpdate(
            title="新标题", context_limit=60, ai_chat_rounds=2, reply_order="connor"
        )
        with (
            patch.object(chatroom_routes, "get_db", side_effect=self._get_db),
            patch.object(chatroom_routes, "load_chatroom_config", return_value={"reply_order": "random"}),
            patch.object(chatroom_routes, "save_chatroom_config"),
            patch.object(chatroom_routes, "_public_chatroom_config", side_effect=lambda cfg: cfg),
            patch.object(chatroom_routes.manager, "broadcast", broadcast),
        ):
            result = await chatroom_routes.patch_room_settings("room-1", body)
            await __import__("asyncio").sleep(0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["room"]["title"], "新标题")
        self.assertEqual(result["room"]["context_limit"], 60)
        self.assertEqual(result["config"]["reply_order"], "connor")
        self.assertGreaterEqual(broadcast.await_count, 1)


if __name__ == "__main__":
    unittest.main()
