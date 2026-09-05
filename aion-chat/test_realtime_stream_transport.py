import unittest

from realtime_stream_transport import consume_realtime_transport
from stream_safety import StreamSafetyResult


class _TTSProbe:
    def __init__(self, *, accepted=False, emitted=False):
        self.accepted_segment_count = 1 if accepted else 0
        self.has_emitted_audio = emitted
        self.discarded = False
        self.cancelled = False
        self.rewound = False

    def discard_pending_text(self):
        self.discarded = True

    def cancel(self):
        self.cancelled = True

    async def rewind_before_audio(self):
        if self.has_emitted_audio:
            return False
        self.rewound = True
        self.accepted_segment_count = 0
        return True


class RealtimeStreamTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_mode_never_calls_safe_consumer(self):
        calls = []

        async def safe_consumer(_source):
            calls.append("safe")
            raise AssertionError("safe consumer must remain isolated")

        async def legacy_consumer(source):
            calls.append(source)
            return StreamSafetyResult("legacy text"), "legacy text"

        outcome = await consume_realtime_transport(
            mode="legacy",
            source_factory=lambda: "legacy",
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: None,
        )

        self.assertEqual(calls, ["legacy"])
        self.assertEqual(outcome.result.committed_text, "legacy text")
        self.assertFalse(outcome.used_fallback)

    async def test_safe_failure_before_audio_resets_and_retries_legacy_once(self):
        calls = []
        resets = []
        tts = _TTSProbe()

        async def safe_consumer(source):
            calls.append(("safe", source))
            return StreamSafetyResult("partial", stop_reason="quality"), "partial"

        async def legacy_consumer(source):
            calls.append(("legacy", source))
            return StreamSafetyResult("complete"), "complete"

        source_number = 0

        def source_factory():
            nonlocal source_number
            source_number += 1
            return source_number

        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=source_factory,
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: resets.append("reset"),
            tts_streamer=tts,
        )

        self.assertEqual(calls, [("safe", 1), ("legacy", 2)])
        self.assertEqual(resets, ["reset"])
        self.assertTrue(tts.rewound)
        self.assertTrue(outcome.used_fallback)
        self.assertFalse(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "complete")

    async def test_safe_failure_after_audio_never_replays(self):
        calls = []
        resets = []
        tts = _TTSProbe(accepted=True, emitted=True)

        async def safe_consumer(source):
            calls.append(("safe", source))
            return StreamSafetyResult("partial", stop_reason="quality"), "partial"

        async def legacy_consumer(source):
            calls.append(("legacy", source))
            return StreamSafetyResult("must not run"), "must not run"

        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=lambda: "source",
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: resets.append("reset"),
            tts_streamer=tts,
        )

        self.assertEqual(calls, [("safe", "source")])
        self.assertEqual(resets, ["reset"])
        self.assertTrue(tts.cancelled)
        self.assertTrue(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "")

    async def test_queued_but_unemitted_audio_is_rewound_then_falls_back(self):
        calls = []
        tts = _TTSProbe(accepted=True, emitted=False)

        async def safe_consumer(source):
            calls.append(("safe", source))
            return StreamSafetyResult("partial", stop_reason="quality"), "partial"

        async def legacy_consumer(source):
            calls.append(("legacy", source))
            return StreamSafetyResult("complete"), "complete"

        serial = iter((1, 2))
        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=lambda: next(serial),
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: None,
            tts_streamer=tts,
        )

        self.assertEqual(calls, [("safe", 1), ("legacy", 2)])
        self.assertTrue(tts.rewound)
        self.assertFalse(outcome.manual_retry_required)

    async def test_failed_legacy_fallback_returns_no_partial_message(self):
        resets = []

        async def safe_consumer(_source):
            return StreamSafetyResult("unsafe", stop_reason="quality"), "unsafe"

        async def legacy_consumer(_source):
            return StreamSafetyResult("legacy partial", stop_reason="transport"), "legacy partial"

        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=lambda: object(),
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: resets.append("reset"),
        )

        self.assertEqual(resets, ["reset", "reset"])
        self.assertTrue(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "")
        self.assertEqual(outcome.visible_text, "")

    async def test_codex_error_text_from_legacy_fallback_is_not_a_saved_reply(self):
        async def safe_consumer(_source):
            return StreamSafetyResult("", stop_reason="transport"), ""

        async def legacy_consumer(_source):
            text = "部分正文[CodexCLI错误] turn failed"
            return StreamSafetyResult(text), text

        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=lambda: object(),
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: None,
        )

        self.assertTrue(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "")

    async def test_gemini_error_text_from_legacy_fallback_is_not_a_saved_reply(self):
        async def safe_consumer(_source):
            return StreamSafetyResult("", stop_reason="transport"), ""

        async def legacy_consumer(_source):
            text = "[Gemini错误 503] upstream unavailable"
            return StreamSafetyResult(text), text

        outcome = await consume_realtime_transport(
            mode="safe_live",
            source_factory=lambda: object(),
            safe_consumer=safe_consumer,
            legacy_consumer=legacy_consumer,
            reset_visible=lambda: None,
        )

        self.assertTrue(outcome.manual_retry_required)
        self.assertEqual(outcome.result.committed_text, "")


if __name__ == "__main__":
    unittest.main()
