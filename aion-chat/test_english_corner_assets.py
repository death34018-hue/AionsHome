import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parent / "static"
STYLESHEET = STATIC_DIR / "english-corner.css"
DOCUMENT = STATIC_DIR / "english-corner.html"


class _DocumentOutlineParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.elements = {}
        self.labels = {}
        self.title_parts = []
        self._stack = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"])
            self.elements[attributes["id"]] = {
                "tag": tag,
                "attributes": attributes,
                "parent_classes": set(
                    self._stack[-1]["attributes"].get("class", "").split()
                )
                if self._stack
                else set(),
            }
        if tag == "label" and attributes.get("for"):
            self.labels[attributes["for"]] = []
        if tag == "title":
            self._in_title = True
        if tag not in {"area", "base", "br", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            self._stack.append({"tag": tag, "attributes": attributes})

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        while self._stack:
            element = self._stack.pop()
            if element["tag"] == tag:
                break

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)


def _css_declarations(source, selector):
    match = re.search(
        rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}",
        source,
    )
    if not match:
        raise AssertionError(f"Missing CSS selector: {selector}")
    declarations = {}
    for declaration in match.group("body").split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        declarations[name.strip()] = value.strip()
    return declarations


def _pixel_value(declarations, property_name):
    value = declarations.get(property_name, "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)px", value)
    if not match:
        raise AssertionError(
            f"{property_name} must use an explicit pixel target, got {value!r}"
        )
    return float(match.group(1))


def _keyframes_body(source, name):
    match = re.search(
        rf"@keyframes\s+{re.escape(name)}\s*\{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"Missing CSS keyframes: {name}")
    return match.group("body")


def _media_body(source, condition):
    marker = f"@media ({condition})"
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"Missing CSS media query: {condition}")
    opening = source.find("{", start + len(marker))
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"Unclosed CSS media query: {condition}")


class EnglishCornerAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = STYLESHEET.read_text(encoding="utf-8")
        cls.html = DOCUMENT.read_text(encoding="utf-8")
        cls.outline = _DocumentOutlineParser()
        cls.outline.feed(cls.html)

    def test_mobile_action_targets_are_at_least_44_pixels_high(self):
        for selector in (
            ".status-banner button",
            ".undo-toast button",
            ".archive-tabs button",
            ".archive-action",
        ):
            with self.subTest(selector=selector):
                declarations = _css_declarations(self.css, selector)
                self.assertGreaterEqual(
                    _pixel_value(declarations, "min-height"),
                    44,
                )

        carousel = _css_declarations(self.css, ".carousel-button")
        self.assertGreaterEqual(_pixel_value(carousel, "width"), 44)
        self.assertGreaterEqual(_pixel_value(carousel, "height"), 44)

    def test_actor_radio_has_visible_keyboard_focus_treatment(self):
        declarations = _css_declarations(
            self.css,
            ".actor-choice:has(input:focus-visible)",
        )
        self.assertNotEqual(declarations.get("outline"), "none")
        self.assertTrue(declarations.get("outline"))
        self.assertTrue(declarations.get("outline-offset"))

    def test_generation_form_exposes_voice_selection_and_fixed_success_notice(self):
        self.assertIn("voiceSelect", self.outline.ids)
        self.assertIn("voiceMeta", self.outline.ids)
        self.assertIn("generationToast", self.outline.ids)
        self.assertIn("voiceSelect", self.outline.labels)
        toast = _css_declarations(self.css, ".generation-toast")
        self.assertEqual(toast.get("position"), "fixed")

    def test_document_uses_the_learning_corner_name(self):
        self.assertEqual(
            "".join(self.outline.title_parts).strip(),
            "学习角",
        )

    def test_card_region_is_clipped_by_a_rounded_scroll_viewport(self):
        region_element = self.outline.elements["cardRegion"]
        self.assertIn("card-viewport", region_element["parent_classes"])

        viewport = _css_declarations(self.css, ".card-viewport")
        region = _css_declarations(self.css, ".card-region")
        self.assertEqual(viewport.get("overflow"), "hidden")
        self.assertEqual(
            viewport.get("border-radius"),
            "var(--learning-card-radius)",
        )
        self.assertEqual(region.get("overflow"), "auto")

    def test_topbar_has_accessible_home_link_and_keeps_archive_button(self):
        home_link = self.outline.elements["homeBackLink"]
        self.assertEqual(home_link["tag"], "a")
        self.assertEqual(home_link["attributes"].get("href"), "/")
        self.assertEqual(
            home_link["attributes"].get("aria-label"),
            "返回主界面",
        )
        self.assertIn("archiveButton", self.outline.ids)

        home_target = _css_declarations(self.css, ".home-back-link")
        self.assertGreaterEqual(_pixel_value(home_target, "min-width"), 44)
        self.assertGreaterEqual(_pixel_value(home_target, "min-height"), 44)

    def test_card_navigation_keyframes_never_animate_opacity(self):
        for name in ("card-enter-next", "card-enter-previous"):
            with self.subTest(name=name):
                body = _keyframes_body(self.css, name)
                self.assertNotIn("opacity", body)

    def test_extreme_narrow_topbar_fits_without_shrinking_action_targets(self):
        narrow_css = _media_body(self.css, "max-width: 23rem")
        topbar = _css_declarations(narrow_css, ".topbar")
        left_actions = _css_declarations(
            narrow_css,
            ".top-left-actions .top-button",
        )
        generate = _css_declarations(narrow_css, ".generate-button")

        self.assertEqual(
            topbar.get("grid-template-columns"),
            "5.75rem minmax(0, 1fr) 2.75rem",
        )
        self.assertEqual(topbar.get("gap"), "0.25rem")
        self.assertGreaterEqual(_pixel_value(left_actions, "min-width"), 44)
        self.assertGreaterEqual(_pixel_value(left_actions, "min-height"), 44)
        self.assertGreaterEqual(_pixel_value(generate, "width"), 44)

    def test_learning_card_typography_is_compact_without_shrinking_audio_targets(self):
        sentence = _css_declarations(self.css, ".sentence-button")
        dialogue = _css_declarations(self.css, ".dialogue")
        translation = _css_declarations(self.css, ".translation")
        audio = _css_declarations(self.css, ".audio-button")

        self.assertEqual(
            sentence.get("font-size"),
            "clamp(0.98rem, 3.5vw, 1.18rem)",
        )
        self.assertEqual(sentence.get("line-height"), "1.45")
        self.assertEqual(
            dialogue.get("gap"),
            "clamp(0.78rem, 2.4vw, 1.08rem)",
        )
        self.assertEqual(translation.get("margin"), "0.28rem 0 0")
        self.assertGreaterEqual(
            float(audio["min-width"].removesuffix("rem")) * 16,
            44,
        )
        self.assertGreaterEqual(
            float(audio["min-height"].removesuffix("rem")) * 16,
            44,
        )


if __name__ == "__main__":
    unittest.main()
