import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class BackgroundStreamSafetyWiringTest(unittest.TestCase):
    def test_background_tts_routes_use_shared_guard(self):
        for name in ("schedule.py", "camera.py", "location.py", "fund.py"):
            with self.subTest(name=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                expected = (
                    "_consume_background_realtime_stream("
                    if name in {"schedule.py", "camera.py"}
                    else "_consume_background_stream("
                )
                self.assertIn(expected, source)
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

    def test_ordinary_camera_analysis_stays_on_legacy_background_consumer(self):
        source = (ROOT / "camera.py").read_text(encoding="utf-8")
        marker = "async def perform_cam_check"
        self.assertIn(marker, source)
        ordinary_section = source[source.index(marker):]
        self.assertIn("_consume_background_stream(", ordinary_section)
        self.assertNotIn("_consume_background_realtime_stream(", ordinary_section)

    def test_schedule_realtime_dispatch_is_gated_to_chat_wakeups(self):
        source = (ROOT / "schedule.py").read_text(encoding="utf-8")
        self.assertIn("use_realtime_wakeup = is_chatroom and not is_proactive", source)
        self.assertGreaterEqual(source.count("if use_realtime_wakeup:"), 2)


if __name__ == "__main__":
    unittest.main()
