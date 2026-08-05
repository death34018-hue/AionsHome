import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class BackgroundStreamSafetyWiringTest(unittest.TestCase):
    def test_background_tts_routes_use_shared_guard(self):
        for name in ("schedule.py", "camera.py", "location.py", "fund.py"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("_consume_background_stream(", source)
                self.assertNotIn("_tts.feed(chunk)", source)
                self.assertNotIn("_tts.feed(visible_chunk)", source)

    def test_scheduler_logs_timeout_type_and_traceback(self):
        source = (ROOT / "schedule.py").read_text(encoding="utf-8")
        self.assertIn(
            "tick_future.result(timeout=CHAT_STREAM_POLICY.total_timeout + 30)",
            source,
        )
        self.assertIn("tick_future.cancel()", source)
        self.assertNotIn("result(timeout=60)", source)
        self.assertIn(
            'log.exception(\n                    "schedule tick error (%s)",',
            source,
        )


if __name__ == "__main__":
    unittest.main()
