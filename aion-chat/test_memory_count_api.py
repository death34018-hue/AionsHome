import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite


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


if __name__ == "__main__":
    unittest.main()
