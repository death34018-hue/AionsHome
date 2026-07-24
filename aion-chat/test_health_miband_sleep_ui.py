import pathlib
import unittest


class MiBandSleepUiTest(unittest.TestCase):
    def test_health_page_renders_each_sleep_session(self):
        source = (pathlib.Path(__file__).parent / "static" / "health.html").read_text(encoding="utf-8")

        self.assertIn('id="miBandSleepSessions"', source)
        self.assertIn("sleep.sessions", source)
        self.assertIn("小睡", source)
        self.assertNotIn("回笼觉", source)

    def test_health_page_renders_recent_activity_windows(self):
        source = (pathlib.Path(__file__).parent / "static" / "health.html").read_text(encoding="utf-8")

        self.assertIn('id="miBandRecent30"', source)
        self.assertIn('id="miBandRecent60"', source)
        self.assertIn("recent30ActivityMinutes", source)
        self.assertIn("recent30Steps", source)
        self.assertIn("recent60ActivityMinutes", source)
        self.assertIn("recent60Steps", source)


if __name__ == "__main__":
    unittest.main()
