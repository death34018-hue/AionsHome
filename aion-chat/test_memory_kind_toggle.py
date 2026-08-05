import sys
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes import memories as memory_routes


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row


class _FakeDb:
    def __init__(self):
        self.row_factory = None
        self.executed = []

    async def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        if "SELECT id FROM memories" in sql:
            return _FakeCursor({"id": "mem-1"})
        return _FakeCursor()

    async def commit(self):
        pass


class _FakeDbContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MainMemoryKindToggleTests(unittest.TestCase):
    def test_patch_memory_kind_updates_type_without_reembedding(self):
        app = FastAPI()
        app.include_router(memory_routes.router)
        client = TestClient(app)
        fake_db = _FakeDb()

        with patch.object(memory_routes, "get_db", return_value=_FakeDbContext(fake_db)), \
             patch.object(memory_routes, "get_embedding", new=AsyncMock()) as get_embedding:
            response = client.patch("/api/memories/mem-1/kind", json={"memory_kind": "daily"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["memory_kind"], "daily")
        get_embedding.assert_not_awaited()

        update_statements = [
            (sql, params) for sql, params in fake_db.executed
            if sql.strip().upper().startswith("UPDATE")
        ]
        self.assertEqual(update_statements, [
            ("UPDATE memories SET type=? WHERE id=?", ("daily", "mem-1"))
        ])
        self.assertTrue(all("embedding" not in sql.lower() for sql, _ in update_statements))


class MemoryKindToggleFrontendTests(unittest.TestCase):
    def test_memory_labels_open_selection_menus_instead_of_toggling_directly(self):
        main_html = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")
        chatroom_js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")

        self.assertIn("openMemoryKindMenu('", main_html)
        self.assertIn("selectMemoryKind('", main_html)
        self.assertNotIn("toggleMemoryKind('", main_html)
        self.assertIn("/api/memories/${id}/kind", main_html)
        self.assertIn("openChatroomMemoryKindMenu('", chatroom_js)
        self.assertIn("selectChatroomMemoryKind('", chatroom_js)
        self.assertNotIn("toggleChatroomMemoryKind('", chatroom_js)
        self.assertIn("memory_kind: nextKind", chatroom_js)

    def test_main_memory_counts_use_server_kind_totals_not_loaded_page_length(self):
        route_source = (ROOT / "routes" / "memories.py").read_text(encoding="utf-8")
        main_html = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")

        self.assertIn('"kind_totals"', route_source)
        self.assertIn('"daily"', route_source)
        self.assertIn('"long_term"', route_source)
        self.assertIn("let _memoryKindTotals", main_html)
        self.assertIn("result.kind_totals", main_html)
        self.assertIn("_memoryKindTotals[_memoryKindFilter]", main_html)
        self.assertNotIn("${filterText} ${kindFiltered.length} 条", main_html)

    def test_main_memory_kind_tab_requests_a_fresh_server_filtered_page(self):
        main_html = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")
        load_function = re.search(
            r"async function loadMemories\(options = \{\}\) \{.*?\n\}",
            main_html,
            re.S,
        ).group(0)
        filter_function = re.search(
            r"function setMemoryKindFilter\(kind\) \{.*?\n\}",
            main_html,
            re.S,
        ).group(0)

        self.assertIn('params.set("kind", _memoryKindFilter)', load_function)
        self.assertIn("loadMemories({ reset: true })", filter_function)
        self.assertNotIn("renderMemories();", filter_function)


if __name__ == "__main__":
    unittest.main()
