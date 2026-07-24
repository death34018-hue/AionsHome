import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ChatroomComposerMobilePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

    def test_input_events_coalesce_height_measurement_once_per_frame(self):
        self.assertIn("let inputResizeFrame = null;", self.js)
        scheduler = re.search(
            r"function scheduleInputResize\(\) \{.*?\n\}",
            self.js,
            re.S,
        )
        self.assertIsNotNone(scheduler)
        body = scheduler.group(0)
        self.assertIn("if (inputResizeFrame !== null) return;", body)
        self.assertIn("requestAnimationFrame", body)
        self.assertIn("inputResizeFrame = null;", body)
        self.assertIn("resizeInput();", body)
        self.assertIn(
            "inputEl.addEventListener('input', scheduleInputResize)",
            self.js,
        )
        self.assertNotIn(
            "inputEl.addEventListener('input', resizeInput)",
            self.js,
        )

    def test_layout_measurement_stays_in_immediate_resize_function(self):
        resize = re.search(
            r"function resizeInput\(\) \{.*?\n\}",
            self.js,
            re.S,
        )
        self.assertIsNotNone(resize)
        self.assertIn("inputEl.scrollHeight", resize.group(0))
        self.assertIn("Math.min(inputEl.scrollHeight, 120)", resize.group(0))
        self.assertEqual(self.js.count("inputEl.scrollHeight"), 1)

    def test_pending_resize_is_cancelled_when_page_is_hidden(self):
        self.assertIn("cancelAnimationFrame(inputResizeFrame)", self.js)
        self.assertIn(
            "window.addEventListener('pagehide', cancelPendingInputResize)",
            self.js,
        )

    def test_chatroom_script_cache_key_includes_composer_fix(self):
        version = "composer-mobile-performance-20260724"
        self.assertRegex(
            self.html,
            rf'chatroom\.js\?v=[^"\n]*{version}',
        )


if __name__ == "__main__":
    unittest.main()
