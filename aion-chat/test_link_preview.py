import unittest

import link_preview
from link_preview import extract_urls, link_preview_from_html, link_preview_fallback
from tts import _strip_tags


class LinkPreviewTests(unittest.TestCase):
    def test_strip_urls_keeps_markdown_label_and_removes_bare_address(self):
        clean = getattr(link_preview, "strip_urls_for_message", lambda text: text)
        text = (
            "可以参考[症状与原因](https://example.com/health/symptoms)。\n"
            "原始资料：https://example.org/a/very/long/path?q=1"
        )

        self.assertEqual(clean(text), "可以参考症状与原因。")

    def test_tts_strips_link_addresses_but_reads_markdown_label(self):
        text = "看看[这份资料](https://example.com/very/long/path)，备用 https://example.org/raw"

        self.assertEqual(_strip_tags(text), "看看这份资料，备用")

    def test_strip_urls_preserves_paragraph_breaks(self):
        text = "第一段。\n\n第二段参考[资料](https://example.com/doc)。"

        self.assertEqual(link_preview.strip_urls_for_message(text), "第一段。\n\n第二段参考资料。")

    def test_extract_urls_strips_trailing_punctuation_and_dedupes(self):
        text = "看看 http://hyena-home.com/，还有 https://example.com/path?q=1。重复：https://example.com/path?q=1"

        self.assertEqual(
            extract_urls(text),
            ["http://hyena-home.com/", "https://example.com/path?q=1"],
        )

    def test_link_preview_from_html_reads_common_metadata(self):
        html = """
        <html>
          <head>
            <meta property="og:title" content="Hyena Home">
            <meta property="og:description" content="A small page for testing.">
            <meta property="og:image" content="/cover.png">
            <meta property="og:site_name" content="Hyena">
            <link rel="shortcut icon" href="/favicon.ico">
          </head>
        </html>
        """

        card = link_preview_from_html("http://hyena-home.com/post", html)

        self.assertEqual(card["type"], "link_preview")
        self.assertEqual(card["url"], "http://hyena-home.com/post")
        self.assertEqual(card["title"], "Hyena Home")
        self.assertEqual(card["description"], "A small page for testing.")
        self.assertEqual(card["site_name"], "Hyena")
        self.assertEqual(card["image"], "http://hyena-home.com/cover.png")
        self.assertEqual(card["favicon"], "http://hyena-home.com/favicon.ico")

    def test_fallback_uses_hostname_when_metadata_is_unavailable(self):
        card = link_preview_fallback("https://example.com/articles/hello")

        self.assertEqual(card["type"], "link_preview")
        self.assertEqual(card["title"], "example.com")
        self.assertEqual(card["site_name"], "example.com")
        self.assertEqual(card["url"], "https://example.com/articles/hello")


if __name__ == "__main__":
    unittest.main()
