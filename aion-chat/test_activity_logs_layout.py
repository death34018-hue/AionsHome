import unittest
from html.parser import HTMLParser
from pathlib import Path


class _ActivePaneParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active_panes = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        if tag == "div" and {"activity-pane", "active"} <= classes:
            self.active_panes.append(values.get("id"))


class ActivityLogsLayoutTest(unittest.TestCase):
    def test_current_status_is_the_only_default_active_pane(self):
        parser = _ActivePaneParser()
        parser.feed(
            Path(__file__).with_name("static").joinpath("activity-logs.html").read_text(encoding="utf-8")
        )

        self.assertEqual(["contextPane"], parser.active_panes)


if __name__ == "__main__":
    unittest.main()
