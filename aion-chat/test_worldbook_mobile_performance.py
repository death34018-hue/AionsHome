import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WorldbookMobilePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "worldbook.html").read_text(encoding="utf-8")

    def test_textarea_input_resize_is_coalesced_per_animation_frame(self):
        self.assertIn("const worldbookResizeFrames = new WeakMap();", self.html)
        scheduler = re.search(
            r"function scheduleWorldbookTextareaResize\(el\) \{.*?\n\}",
            self.html,
            re.S,
        )
        self.assertIsNotNone(scheduler)
        self.assertIn("requestAnimationFrame", scheduler.group(0))
        self.assertIn("scheduleWorldbookTextareaResize(textarea);", self.html)
        self.assertIn("scheduleWorldbookTextareaResize(creativeTextarea);", self.html)

    def test_soft_keyboard_height_resize_does_not_measure_active_panel(self):
        self.assertIn("let worldbookViewportWidth = window.innerWidth;", self.html)
        handler = re.search(
            r"function handleWorldbookViewportResize\(\) \{.*?\n\}",
            self.html,
            re.S,
        )
        self.assertIsNotNone(handler)
        self.assertIn("if (nextWidth === worldbookViewportWidth) return;", handler.group(0))
        self.assertIn("worldbookViewportWidth = nextWidth;", handler.group(0))

    def test_hidden_preview_returns_before_compiling_all_sections(self):
        self.assertIn("let worldbookPreviewDirty = true;", self.html)
        self.assertIn("function schedulePreviewSync()", self.html)
        sync = re.search(r"function syncPreview\(\) \{(?P<body>.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(sync)
        body = sync.group("body")
        guard = 'if (!$("panelPreview")?.classList.contains("active")) {'
        self.assertIn(guard, body)
        self.assertLess(body.index(guard), body.index('compileSections("ai", AI_SECTIONS)'))
        self.assertIn("worldbookPreviewDirty = false;", body)


if __name__ == "__main__":
    unittest.main()
