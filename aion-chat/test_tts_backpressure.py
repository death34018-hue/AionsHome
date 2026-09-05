import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tts


class TTSBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewind_before_audio_clears_started_workers_for_fallback(self):
        blocker = asyncio.Event()

        async def slow_request(_text, _voice, *, seq=0):
            await blocker.wait()
            return b"audio"

        streamer = tts.TTSStreamer(
            "rewind_before_audio",
            "voice",
            None,
            min_chars=1,
            max_chars=2,
        )
        with patch("tts._request_tts_audio", side_effect=slow_request):
            await streamer.feed_async("一句。")
            self.assertGreater(streamer.accepted_segment_count, 0)
            self.assertFalse(streamer.has_emitted_audio)

            self.assertTrue(await streamer.rewind_before_audio())
            self.assertEqual(streamer.accepted_segment_count, 0)
            self.assertEqual(streamer.worker_task_count, 0)

            blocker.set()
            await streamer.feed_async("重试。")
            await streamer.flush()

        self.assertTrue(streamer.has_emitted_audio)

    async def test_notification_attempt_is_already_an_irreversible_checkpoint(self):
        notify_started = asyncio.Event()
        release_notify = asyncio.Event()

        async def request_audio(_text, _voice, *, seq=0):
            return b"audio"

        async def blocked_notify(_payload):
            notify_started.set()
            await release_notify.wait()

        streamer = tts.TTSStreamer(
            "notify_checkpoint",
            "voice",
            None,
            min_chars=1,
            max_chars=2,
        )
        with (
            patch("tts._request_tts_audio", side_effect=request_audio),
            patch.object(streamer, "_notify", side_effect=blocked_notify),
        ):
            await streamer.feed_async("一句。")
            await notify_started.wait()

            self.assertTrue(streamer.has_emitted_audio)
            self.assertFalse(await streamer.rewind_before_audio())

            release_notify.set()
            await streamer.flush()

    async def test_discard_pending_text_prevents_unspoken_text_from_being_synthesized(self):
        calls = []

        async def record_audio(text, *args, **kwargs):
            calls.append(text)
            return b"audio"

        with tempfile.TemporaryDirectory() as td:
            streamer = tts.TTSStreamer(
                "msg_retry",
                "voice",
                low_latency_first_chunk=True,
                cache_dir=Path(td),
                cache_max_bytes=None,
            )
            streamer.feed("尚未形成句子的待回退正文")
            streamer.discard_pending_text()
            with patch.object(tts, "_request_tts_audio", new=record_audio):
                await streamer.flush()

        self.assertEqual(calls, [])
        self.assertFalse(streamer.has_emitted_audio)

    async def test_successful_audio_notification_sets_irreversible_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            streamer = tts.TTSStreamer(
                "msg_emitted",
                "voice",
                min_chars=1,
                max_chars=2,
                cache_dir=Path(td),
                cache_max_bytes=None,
            )
            with patch.object(
                tts,
                "_request_tts_audio",
                new=lambda *args, **kwargs: asyncio.sleep(0, result=b"audio"),
            ):
                await streamer.feed_async("好。")
                await streamer.flush()

        self.assertTrue(streamer.has_emitted_audio)

    async def test_streaming_feed_never_runs_more_than_two_requests(self):
        active = 0
        peak_active = 0
        release = asyncio.Event()

        async def blocked_audio(*args, **kwargs):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await release.wait()
            active -= 1
            return b"audio"

        with tempfile.TemporaryDirectory() as td:
            streamer = tts.TTSStreamer(
                "msg_bounded",
                "voice",
                min_chars=1,
                max_chars=2,
                cache_dir=Path(td),
                cache_max_bytes=None,
                max_concurrency=2,
                max_pending_segments=6,
                max_segments=40,
            )
            with patch.object(tts, "_request_tts_audio", new=blocked_audio):
                producer = asyncio.create_task(streamer.feed_async("好。" * 20))
                for _ in range(100):
                    if active == 2 and streamer.pending_segment_count == 6:
                        break
                    await asyncio.sleep(0)

                self.assertEqual(peak_active, 2)
                self.assertEqual(streamer.worker_task_count, 2)
                self.assertEqual(streamer.pending_segment_count, 6)
                self.assertFalse(producer.done())

                release.set()
                await producer
                await streamer.flush()

        self.assertLessEqual(peak_active, 2)

    async def test_huge_sync_input_cannot_create_one_task_per_segment(self):
        with tempfile.TemporaryDirectory() as td:
            streamer = tts.TTSStreamer(
                "msg_huge",
                "voice",
                min_chars=100,
                max_chars=200,
                cache_dir=Path(td),
                cache_max_bytes=None,
                max_segments=40,
            )

            streamer.feed("正常正文。" * 60_000)

            self.assertEqual(streamer.worker_task_count, 0)
            self.assertEqual(streamer.accepted_segment_count, 0)

            with patch.object(
                tts,
                "_request_tts_audio",
                new=lambda *args, **kwargs: asyncio.sleep(0, result=b"audio"),
            ):
                await streamer.flush()

            self.assertEqual(streamer.accepted_segment_count, 40)
            self.assertLessEqual(streamer.worker_task_count, 2)
            self.assertTrue(streamer.segment_limit_reached)

    async def test_cancel_discards_queued_segments(self):
        calls = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_audio(*args, **kwargs):
            nonlocal calls
            calls += 1
            first_started.set()
            await release.wait()
            return b"audio"

        with tempfile.TemporaryDirectory() as td:
            streamer = tts.TTSStreamer(
                "msg_cancel_queue",
                "voice",
                min_chars=1,
                max_chars=2,
                cache_dir=Path(td),
                cache_max_bytes=None,
                max_concurrency=1,
                max_pending_segments=6,
            )
            with patch.object(tts, "_request_tts_audio", new=blocked_audio):
                producer = asyncio.create_task(streamer.feed_async("好。" * 7))
                await first_started.wait()
                streamer.cancel()
                release.set()
                await producer
                await streamer.flush()

        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
