import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ChatroomPersonaMobilePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        cls.html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

    def test_persona_textarea_input_resizes_at_most_once_per_animation_frame(self):
        self.assertIn("const crPersonaResizeFrames = new WeakMap();", self.js)
        self.assertIn("function crSchedulePersonaTextareaResize(el)", self.js)
        scheduler = re.search(
            r"function crSchedulePersonaTextareaResize\(el\) \{.*?\n\}",
            self.js,
            re.S,
        )
        self.assertIsNotNone(scheduler)
        self.assertIn("requestAnimationFrame", scheduler.group(0))
        self.assertIn(
            "textarea.addEventListener('input', () => crSchedulePersonaTextareaResize(textarea));",
            self.js,
        )

    def test_soft_keyboard_height_resize_does_not_measure_every_persona_field(self):
        self.assertIn("let crPersonaViewportWidth = window.innerWidth;", self.js)
        handler = re.search(
            r"function crHandlePersonaViewportResize\(\) \{.*?\n\}",
            self.js,
            re.S,
        )
        self.assertIsNotNone(handler)
        self.assertIn("if (nextWidth === crPersonaViewportWidth) return;", handler.group(0))
        self.assertIn("crPersonaViewportWidth = nextWidth;", handler.group(0))

    def test_mobile_persona_overlay_avoids_nested_backdrop_filters(self):
        mobile = re.search(
            r"@media \(max-width: 760px\) and \(pointer: coarse\) \{(?P<body>.*?)\n\}",
            self.css,
            re.S,
        )
        self.assertIsNotNone(mobile)
        rules = mobile.group("body")
        self.assertRegex(
            rules,
            r"\.persona-page-overlay\.active\s*\{[^}]*backdrop-filter:\s*none;[^}]*-webkit-backdrop-filter:\s*none;",
        )
        self.assertRegex(
            rules,
            r"\.persona-page-panel\s*\{[^}]*background:\s*var\(--bg\);[^}]*backdrop-filter:\s*none;[^}]*-webkit-backdrop-filter:\s*none;",
        )

    def test_chatroom_assets_are_cache_busted_for_the_persona_performance_fix(self):
        version = "persona-mobile-performance-20260721"
        self.assertEqual(self.html.count(version), 2)
        self.assertRegex(self.html, rf'chatroom\.css\?v=[^"\n]*{version}')
        self.assertRegex(self.html, rf'chatroom\.js\?v=[^"\n]*{version}')


if __name__ == "__main__":
    unittest.main()
