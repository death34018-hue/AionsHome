import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tts
import voice


class VoiceLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_low_latency_dispatches_first_sentence_without_shortening_theater(self):
        first_sentence = "宝宝，我已经听见你刚才说的话了。"
        followup = "后面的内容还没有累积到普通分段长度，所以现在不应该提前送去合成。"

        async def fake_audio(*args, **kwargs):
            return b"audio"

        with tempfile.TemporaryDirectory() as td, patch.object(
            tts, "_request_tts_audio", new=fake_audio
        ):
            chat = tts.TTSStreamer(
                "chat_latency",
                "voice",
                low_latency_first_chunk=True,
                cache_dir=Path(td),
                cache_max_bytes=None,
            )
            await chat.feed_async(first_sentence + followup)
            self.assertEqual(chat.accepted_segment_count, 1)
            await chat.flush()

            theater = tts.TTSStreamer(
                "theater_latency",
                "voice",
                min_chars=300,
                max_chars=500,
                cache_dir=Path(td),
                cache_max_bytes=None,
            )
            await theater.feed_async(first_sentence + followup)
            self.assertEqual(theater.accepted_segment_count, 0)
            await theater.flush()

    async def test_pc_command_silence_adapts_to_utterance_length(self):
        self.assertEqual(voice.command_silence_frames(30), 27)
        self.assertEqual(voice.command_silence_frames(100), 40)


if __name__ == "__main__":
    unittest.main()
