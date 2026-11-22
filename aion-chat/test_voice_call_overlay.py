import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


class VoiceCallOverlayTests(unittest.TestCase):
    def test_tts_chunk_payload_includes_caption_text(self):
        async def run():
            import tts

            queue = asyncio.Queue()

            async def fake_request(text, voice, *, seq=None):
                return b"mp3"

            with patch.object(tts, "_request_tts_audio", fake_request):
                streamer = tts.TTSStreamer(
                    "vc_test",
                    "voice://test",
                    ws_manager=None,
                    sse_queue=queue,
                    min_chars=1,
                    max_chars=20,
                    cache_dir=ROOT / "data" / "test_tts_cache",
                )
                streamer.feed("First sentence. Second sentence.")
                await streamer.flush()

            chunks = []
            while not queue.empty():
                item = await queue.get()
                if item.get("type") == "tts_chunk":
                    chunks.append(item["data"])

            self.assertGreaterEqual(len(chunks), 1)
            self.assertEqual(chunks[0]["text"], "First sentence.")
            self.assertTrue(all("text" in chunk for chunk in chunks))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
