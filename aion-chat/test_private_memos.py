import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import HTTPException

from routes import private_memos as routes


class PrivateMemoRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memos.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE private_memos (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'app',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                )
            """)
            await db.commit()

        @asynccontextmanager
        async def temporary_db():
            async with aiosqlite.connect(self.db_path) as db:
                yield db

        self.db_patch = patch.object(routes, "get_db", temporary_db)
        self.broadcast_patch = patch.object(
            routes.manager, "broadcast", new=AsyncMock()
        )
        self.db_patch.start()
        self.broadcast = self.broadcast_patch.start()

    async def asyncTearDown(self):
        self.broadcast_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_create_is_idempotent_and_lists_active(self):
        body = routes.PrivateMemoCreate(
            id="memo-1", content="  买牛奶  ", source="widget", created_at=100.0
        )
        first = await routes.create_private_memo(body)
        second = await routes.create_private_memo(body)

        self.assertEqual(first["content"], "买牛奶")
        self.assertEqual(second["id"], "memo-1")
        self.assertEqual(
            [item["id"] for item in await routes.list_private_memos("active")],
            ["memo-1"],
        )

    async def test_edit_complete_and_restore(self):
        await routes.create_private_memo(
            routes.PrivateMemoCreate(id="memo-2", content="旧内容")
        )
        edited = await routes.update_private_memo(
            "memo-2", routes.PrivateMemoUpdate(content=" 新内容 ")
        )
        completed = await routes.update_private_memo(
            "memo-2", routes.PrivateMemoUpdate(status="completed")
        )
        restored = await routes.update_private_memo(
            "memo-2", routes.PrivateMemoUpdate(status="active")
        )

        self.assertEqual(edited["content"], "新内容")
        self.assertIsNotNone(completed["completed_at"])
        self.assertIsNone(restored["completed_at"])

    async def test_delete_removes_record_and_missing_update_is_404(self):
        await routes.create_private_memo(
            routes.PrivateMemoCreate(id="memo-3", content="删除我")
        )
        self.assertEqual(await routes.delete_private_memo("memo-3"), {"ok": True})
        self.assertEqual(await routes.list_private_memos("active"), [])
        with self.assertRaises(HTTPException) as raised:
            await routes.update_private_memo(
                "missing", routes.PrivateMemoUpdate(status="completed")
            )
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
