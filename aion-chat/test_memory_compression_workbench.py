import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _ButtonIdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.button_ids = set()

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            values = dict(attrs)
            if values.get("id"):
                self.button_ids.add(values["id"])


class MemoryCompressionWorkbenchTests(unittest.TestCase):
    def test_backend_exposes_preview_and_run_routes(self):
        source = (ROOT / "routes" / "memories.py").read_text(encoding="utf-8")

        self.assertIn('/api/memories/calendar-compression/preview', source)
        self.assertIn('/api/memories/calendar-compression/jobs', source)
        self.assertIn('/api/memories/calendar-compression/jobs/{job_id}', source)
        self.assertIn("model_key", source)

    def test_standalone_page_has_model_store_and_candidate_controls(self):
        source = (ROOT / "static" / "memory-compression.html").read_text(encoding="utf-8")

        self.assertIn('id="compressionModel"', source)
        self.assertIn('id="compressionTarget"', source)
        self.assertIn('data-level="daily"', source)
        self.assertIn('data-level="weekly"', source)
        self.assertIn('data-level="monthly"', source)
        self.assertIn("memory_count", source)
        self.assertIn("button.disabled = !preview.can_run", source)
        self.assertIn("/api/models", source)
        self.assertIn("pollCompressionJobs", source)
        self.assertIn("job.status === \"running\"", source)

    def test_page_is_registered_as_a_document_route(self):
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        manifest_source = (ROOT / "asset_manifest.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/memory-compression")', main_source)
        self.assertIn('"/memory-compression": "memory-compression.html"', manifest_source)

    def test_memory_page_links_to_compression_workbench(self):
        source = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")

        self.assertIn('href="/memory-compression"', source)

    def test_memory_page_does_not_show_legacy_compression_draft_button(self):
        source = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")
        parser = _ButtonIdParser()
        parser.feed(source)

        self.assertNotIn("compressDailyBtn", parser.button_ids)

    def test_chatroom_memory_panel_does_not_show_legacy_daily_compression_button(self):
        source = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")
        parser = _ButtonIdParser()
        parser.feed(source)

        self.assertNotIn("chatroomCompressDailyBtn", parser.button_ids)

    def test_memory_page_discards_stale_search_responses_and_old_count_snapshots(self):
        source = (ROOT / "static" / "memory.html").read_text(encoding="utf-8")

        self.assertIn("MEMORY_SNAPSHOT_KEY = \"memory_page_snapshot_v3\"", source)
        self.assertIn("let _memoryRequestId = 0", source)
        self.assertIn("requestId !== _memoryRequestId", source)

    def test_workbench_uses_shared_theme_and_compact_layout(self):
        source = (ROOT / "static" / "memory-compression.html").read_text(encoding="utf-8")

        self.assertIn('/static/common.css', source)
        self.assertIn('id="aion-theme-css"', source)
        self.assertIn('/static/theme.js', source)
        self.assertIn('class="sub-page"', source)
        self.assertIn('class="top-bar"', source)
        self.assertIn('grid-template-columns: repeat(3', source)
        self.assertIn('<details class="period-details"', source)
        self.assertIn('grid-template-columns: 1fr;', source)
        self.assertNotIn('scroll-snap-type: x mandatory', source)
        self.assertNotIn('grid-auto-flow: column', source)


if __name__ == "__main__":
    unittest.main()
