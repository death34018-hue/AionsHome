import unittest
from pathlib import Path


class TheaterPlayerUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent / "static" / "theater.html").read_text(encoding="utf-8")

    def test_top_player_is_loaded_below_header_with_all_controls(self):
        self.assertIn('<script src="/static/theater_tts_queue.js', self.html)
        header_end = self.html.index("</div>", self.html.index('<div class="chat-header">'))
        player_start = self.html.index('id="ttsTopPlayer"')
        messages_start = self.html.index('id="messages"')
        self.assertLess(header_end, player_start)
        self.assertLess(player_start, messages_start)
        for element_id in (
            "ttsTopPlayer",
            "ttsTopToggle",
            "ttsTopCurrent",
            "ttsTopSeek",
            "ttsTopDuration",
            "ttsTopStop",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_open_player_pushes_messages_below_it(self):
        self.assertIn(".chat-area.tts-player-open .messages", self.html)
        self.assertRegex(
            self.html,
            r"\.tts-top-player\s*\{[^}]*top:\s*calc\(60px \+ env\(safe-area-inset-top\)\)",
        )

    def test_player_has_merged_handoff_and_seek_handlers(self):
        self.assertIn("function handleTTSMerged", self.html)
        self.assertIn("function startMergedPlayback", self.html)
        self.assertIn("function toggleTopTTS", self.html)
        self.assertIn("function seekTopTTS", self.html)

    def test_merged_playback_is_requested_during_the_click_gesture(self):
        self.assertRegex(
            self.html,
            r"audio\.load\(\);\s*requestMergedPlay\(\);",
        )

        replay_start = self.html.index("async function replayTTS(msgId)")
        fallback_start = self.html.index("async function replayTTSFromSegments(")
        primary_replay = self.html[replay_start:fallback_start]
        self.assertIn("startMergedPlayback(msgId, mergedUrl", primary_replay)
        self.assertNotIn("fetch(mergedUrl", primary_replay)

    def test_segment_fallback_uses_server_manifest_without_probe_limits(self):
        fallback_start = self.html.index("async function replayTTSFromSegments(")
        fallback_end = self.html.index("function playManualReplayNext()", fallback_start)
        fallback = self.html[fallback_start:fallback_end]

        self.assertIn("/api/theater/tts/segments/${safeId}", fallback)
        self.assertNotIn("method: 'HEAD'", fallback)
        self.assertNotIn("seq < 200", fallback)
        self.assertNotIn("misses >= 8", fallback)


if __name__ == "__main__":
    unittest.main()
