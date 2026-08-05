import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tts


class TTSBackpressureTests(unittest.IsolatedAsyncioTestCase):
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
