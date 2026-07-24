import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeManager:
    def __init__(self):
        self.aion = "private"
        self.connor = None

    def set_aion_last_active(self, target):
        self.aion = target

    def set_connor_last_active(self, room_id):
        self.connor = room_id


class ActiveWindowPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE runtime_state ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE chatroom_rooms ("
                "id TEXT PRIMARY KEY, type TEXT NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE messages ("
                "id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE chatroom_messages ("
                "id TEXT PRIMARY KEY, room_id TEXT NOT NULL, sender TEXT NOT NULL, "
                "created_at REAL NOT NULL)"
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        return aiosqlite.connect(self.db_path)

    async def test_private_send_persists_and_restores_aion_private(self):
        import active_window_state as state

        current = FakeManager()
        with patch.object(state, "get_db", new=self._get_db):
            await state.record_aion_private_active(manager_obj=current)

            restarted = FakeManager()
            restarted.aion = "chatroom:old-room"
            restored = await state.restore_active_windows(manager_obj=restarted)

        self.assertEqual(current.aion, "private")
        self.assertEqual(restarted.aion, "private")
        self.assertEqual(restored["aion_last_active"], "private")

    async def test_group_send_persists_and_restores_both_routes(self):
        import active_window_state as state

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO chatroom_rooms (id, type) VALUES (?, ?)", ("group-1", "group"))
            await db.commit()

        with patch.object(state, "get_db", new=self._get_db):
            await state.record_chatroom_active("group-1", "group", manager_obj=FakeManager())
            restarted = FakeManager()
            restored = await state.restore_active_windows(manager_obj=restarted)

        self.assertEqual(restarted.aion, "chatroom:group-1")
        self.assertEqual(restarted.connor, "group-1")
        self.assertEqual(restored["aion_last_active"], "chatroom:group-1")
        self.assertEqual(restored["connor_last_active"], "group-1")

    async def test_connor_private_send_does_not_change_aion_route(self):
        import active_window_state as state

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO chatroom_rooms (id, type) VALUES (?, ?)",
                ("connor-1", "connor_1v1"),
            )
            await db.execute(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
                ("aion_last_active", "chatroom:group-old", 1),
            )
            await db.commit()

        manager = FakeManager()
        manager.aion = "chatroom:group-old"
        with patch.object(state, "get_db", new=self._get_db):
            await state.record_chatroom_active("connor-1", "connor_1v1", manager_obj=manager)

        self.assertEqual(manager.aion, "chatroom:group-old")
        self.assertEqual(manager.connor, "connor-1")

    async def test_restore_discards_deleted_room_and_falls_back_safely(self):
        import active_window_state as state

        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
                [
                    ("aion_last_active", "chatroom:deleted", 1),
                    ("connor_last_active", "deleted", 1),
                ],
            )
            await db.commit()

        restarted = FakeManager()
        restarted.aion = "chatroom:wrong"
        restarted.connor = "wrong"
        with patch.object(state, "get_db", new=self._get_db):
            restored = await state.restore_active_windows(manager_obj=restarted)

        self.assertEqual(restarted.aion, "private")
        self.assertIsNone(restarted.connor)
        self.assertEqual(restored["aion_last_active"], "private")
        self.assertIsNone(restored["connor_last_active"])

        db = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0], 0)
        finally:
            db.close()

    async def test_missing_state_is_inferred_from_latest_user_message_windows(self):
        import active_window_state as state

        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO chatroom_rooms (id, type) VALUES (?, ?)",
                [("group-1", "group"), ("connor-1", "connor_1v1")],
            )
            await db.execute(
                "INSERT INTO messages (id, role, created_at) VALUES (?, ?, ?)",
                ("private-user", "user", 100),
            )
            await db.executemany(
                "INSERT INTO chatroom_messages (id, room_id, sender, created_at) VALUES (?, ?, ?, ?)",
                [
                    ("group-user", "group-1", "user", 300),
                    ("connor-user", "connor-1", "user", 200),
                ],
            )
            await db.commit()

        restarted = FakeManager()
        with patch.object(state, "get_db", new=self._get_db):
            restored = await state.restore_active_windows(manager_obj=restarted)

        self.assertEqual(restarted.aion, "chatroom:group-1")
        self.assertEqual(restarted.connor, "group-1")
        self.assertEqual(restored["aion_last_active"], "chatroom:group-1")
        self.assertEqual(restored["connor_last_active"], "group-1")

    async def test_database_initialization_creates_runtime_state_table(self):
        import database

        init_db_path = Path(self.tmp.name) / "init.db"
        with patch.object(database, "DB_PATH", init_db_path):
            await database.init_db()

        db = sqlite3.connect(init_db_path)
        try:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_state'"
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(row, ("runtime_state",))


class ActiveWindowWiringTests(unittest.TestCase):
    def test_routes_use_persistent_recorders_and_no_longer_set_routes_directly(self):
        from routes import chat, chatroom

        chat_source = inspect.getsource(chat)
        chatroom_source = inspect.getsource(chatroom)

        self.assertGreaterEqual(chat_source.count("await record_aion_private_active()"), 2)
        self.assertGreaterEqual(chatroom_source.count("await record_chatroom_active("), 2)
        self.assertNotIn("manager.set_aion_last_active(", chat_source)
        self.assertNotIn("manager.set_aion_last_active(", chatroom_source)
        self.assertNotIn("manager.set_connor_last_active(", chatroom_source)

    def test_startup_restores_state_immediately_after_database_initialization(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        init_pos = source.index("await init_db()")
        restore_pos = source.index("await restore_active_windows()")
        schedule_pos = source.index("schedule_mgr.start()")

        self.assertLess(init_pos, restore_pos)
        self.assertLess(restore_pos, schedule_pos)


if __name__ == "__main__":
    unittest.main()
