import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes import memories as memory_routes


class MemoryCountApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_keeps_global_totals_separate_from_matching_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE memories ("
                    "id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL, "
                    "source_conv TEXT, keywords TEXT, importance REAL, source_start_ts REAL, "
                    "source_end_ts REAL, unresolved INTEGER, source_msg_id TEXT, "
                    "evidence_summary TEXT, evidence_detail_level TEXT, archive_state TEXT)"
                )
                await db.executemany(
                    "INSERT INTO memories (id,content,type,created_at,archive_state) "
                    "VALUES (?,?,?,?,?)",
                    [
                        ("daily-match", "needle", "daily", 3, "active"),
                        ("daily-other", "other", "daily", 2, "active"),
                        ("important-other", "important", "important", 1, "active"),
                    ],
                )
                await db.commit()

            class DbContext:
                async def __aenter__(self):
                    self.db = await aiosqlite.connect(db_path)
                    return self.db

                async def __aexit__(self, exc_type, exc, tb):
                    await self.db.close()

            with patch.object(memory_routes, "get_db", side_effect=lambda: DbContext()):
                result = await memory_routes.list_memories(limit=50, before=None, q="needle")

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["kind_totals"], {"all": 3, "daily": 2, "long_term": 1})
        self.assertEqual(result["filtered_total"], 1)
        self.assertEqual([item["id"] for item in result["items"]], ["daily-match"])

    async def test_kind_filter_is_applied_before_page_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE memories ("
                    "id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL, "
                    "source_conv TEXT, keywords TEXT, importance REAL, source_start_ts REAL, "
                    "source_end_ts REAL, unresolved INTEGER, source_msg_id TEXT, "
                    "evidence_summary TEXT, evidence_detail_level TEXT, archive_state TEXT)"
                )
                await db.executemany(
                    "INSERT INTO memories (id,content,type,created_at,archive_state) "
                    "VALUES (?,?,?,?,?)",
                    [
                        (f"daily-{index}", f"daily {index}", "daily", 1000 - index, "active")
                        for index in range(55)
                    ]
                    + [
                        ("important-3", "important 3", "important", 903, "active"),
                        ("important-2", "important 2", "important", 902, "active"),
                        ("important-1", "important 1", "important", 901, "active"),
                    ],
                )
                await db.commit()

            class DbContext:
                async def __aenter__(self):
                    self.db = await aiosqlite.connect(db_path)
                    return self.db

                async def __aexit__(self, exc_type, exc, tb):
                    await self.db.close()

            app = FastAPI()
            app.include_router(memory_routes.router)
            client = TestClient(app)
            with patch.object(memory_routes, "get_db", side_effect=lambda: DbContext()):
                response = client.get("/api/memories?limit=50&kind=long_term")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [item["id"] for item in payload["items"]],
            ["important-3", "important-2", "important-1"],
        )
        self.assertFalse(payload["has_more"])


if __name__ == "__main__":
    unittest.main()
