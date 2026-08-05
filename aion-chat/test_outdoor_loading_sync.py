import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AssetBundleContractTests(unittest.TestCase):
    def test_manifest_contains_navigable_html_and_schema_two(self):
        from asset_manifest import get_client_asset_manifest

        manifest = get_client_asset_manifest()
        self.assertEqual(manifest["schema"], 2)
        self.assertIn("/", manifest["files"])
        self.assertIn("/memory", manifest["files"])
        self.assertIn("/moments", manifest["files"])
        self.assertIn("/chatroom", manifest["files"])
        self.assertEqual(manifest["files"]["/memory"]["category"], "document")

    def test_android_cache_uses_staging_activation_and_canonical_urls(self):
        source = (WORKSPACE / "AionApp/app/src/main/java/com/aion/chat/SharedAssetCache.java").read_text(encoding="utf-8")

        self.assertIn("staging", source)
        self.assertIn("active-version", source)
        self.assertIn("canonicalPath", source)
        self.assertNotIn("uri.getQuery() != null", source)


class DataLoadingContractTests(unittest.TestCase):
    def test_large_json_and_static_responses_enable_gzip(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("GZipMiddleware", main)
        self.assertIn("minimum_size=1000", main)

    def test_memory_api_is_cursor_paginated_at_fifty(self):
        source = (ROOT / "routes/memories.py").read_text(encoding="utf-8")

        self.assertIn("limit: int = Query(50", source)
        self.assertIn("before: Optional[float]", source)
        self.assertIn('"has_more"', source)
        self.assertIn('"next_cursor"', source)

class ChatroomSettingsContractTests(unittest.TestCase):
    def test_settings_use_one_aggregate_endpoint(self):
        backend = (ROOT / "routes/chatroom.py").read_text(encoding="utf-8")
        frontend = (ROOT / "static/chatroom.js").read_text(encoding="utf-8")

        self.assertIn('@router.get("/rooms/{room_id}/settings")', backend)
        self.assertIn('@router.patch("/rooms/{room_id}/settings")', backend)
        save_block = frontend.split("async function saveSettings()", 1)[1].split("async function triggerDigest", 1)[0]
        self.assertIn("/settings", save_block)
        self.assertNotIn("await loadMessages()", save_block)
        self.assertNotIn("await loadRooms()", save_block)


class DurableSyncContractTests(unittest.TestCase):
    def test_sync_event_table_and_delta_route_exist(self):
        database = (ROOT / "database.py").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        sync_module = ROOT / "sync_events.py"
        sync_route = ROOT / "routes/sync.py"

        self.assertIn("CREATE TABLE IF NOT EXISTS sync_events", database)
        self.assertTrue(sync_module.is_file())
        self.assertTrue(sync_route.is_file())
        self.assertIn("sync_routes.router", main)

    def test_frontends_reconcile_sync_events_after_reconnect(self):
        common = (ROOT / "static/common.js").read_text(encoding="utf-8")
        memory = (ROOT / "static/memory.html").read_text(encoding="utf-8")
        moments = (ROOT / "static/moments.html").read_text(encoding="utf-8")
        chatroom = (ROOT / "static/chatroom.js").read_text(encoding="utf-8")

        self.assertIn("reconcileCommonSync", common)
        self.assertIn("sync_event", memory)
        self.assertIn("sync_event", moments)
        self.assertIn("sync_event", chatroom)


if __name__ == "__main__":
    unittest.main()
