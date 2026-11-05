"""One synthesis entry point for free speech, manual reading and streaming."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import tts
from routes import settings


async def edge_chunks():
    yield {"type": "WordBoundary", "text": "晚安"}
    yield {"type": "audio", "data": b"mp3-first"}
    yield {"type": "audio", "data": b"mp3-last"}


class TTSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_replay_lists_segments_with_one_request_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as td, patch.object(settings, "TTS_CACHE_DIR", Path(td)):
            for name in ("m_s0.mp3", "m_s10.mp3", "m_s2.mp3", "other_s1.mp3"):
                (Path(td) / name).write_bytes(b"audio")
            self.assertTrue(hasattr(settings, "tts_segments"))
            result = await settings.tts_segments("m")
            self.assertEqual(result["urls"], ["/api/tts/audio/m_s0", "/api/tts/audio/m_s2", "/api/tts/audio/m_s10"])

    async def test_free_voice_needs_no_key_and_uses_existing_stream_cache(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("tts.get_key", side_effect=AssertionError("Free TTS must not read paid credentials")), \
             patch("edge_tts.Communicate") as communicate:
            communicate.return_value.stream.side_effect = edge_chunks
            events = asyncio.Queue()
            streamer = tts.TTSStreamer("edge_test", "edge:zh-CN-XiaoxiaoNeural",
                                       sse_queue=events, cache_dir=Path(td), cache_max_bytes=None)
            streamer.feed("今晚的月色很温柔。")
            await streamer.flush()
            self.assertEqual((Path(td) / "edge_test_s0.mp3").read_bytes(), b"mp3-firstmp3-last")
            event = await events.get()
            self.assertEqual(event["type"], "tts_chunk")
            self.assertEqual(event["data"]["url"], "/api/tts/audio/edge_test_s0")
            self.assertEqual(communicate.call_args.kwargs["voice"], "zh-CN-XiaoxiaoNeural")

    async def test_manual_reading_uses_same_dispatch_and_cache(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(settings, "TTS_CACHE_DIR", Path(td)), \
             patch("tts.get_key", return_value=""), \
             patch.object(settings, "get_key", return_value=""), \
             patch("edge_tts.Communicate") as communicate:
            communicate.return_value.stream.side_effect = edge_chunks
            response = await settings.tts_synthesize(settings.TTSRequest(
                text="晚安。", voice="edge:zh-CN-XiaoxiaoNeural", msg_id="manual_edge"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body, b"mp3-firstmp3-last")
            self.assertEqual((Path(td) / "manual_edge.mp3").read_bytes(), response.body)

    async def test_free_voices_survive_missing_or_failing_paid_service(self):
        favorites = {"edge_tts_favorites": ["edge:zh-CN-XiaoxiaoNeural"]}
        with patch.object(settings, "SETTINGS", favorites), \
             patch.object(settings, "get_key", return_value=""):
            result = await settings.tts_voice_list()
        self.assertEqual([v["uri"] for v in result["voices"]], favorites["edge_tts_favorites"])
        with patch.object(settings, "SETTINGS", favorites), \
             patch.object(settings, "get_key", return_value="test"), \
             patch.object(settings.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=RuntimeError("offline"))
            failed = await settings.tts_voice_list()
        self.assertEqual(result["voices"], failed["voices"])

    async def test_catalog_keeps_all_voices_but_candidates_only_include_favorites(self):
        with patch.object(settings, "SETTINGS", {}), \
             patch.object(settings, "get_key", return_value=""), \
             patch.object(settings, "save_settings") as save:
            self.assertEqual((await settings.tts_voice_list())["voices"], [])
            catalog = await settings.edge_voice_catalog()
            self.assertEqual(len(catalog["voices"]), len(tts.EDGE_VOICES))
            voice = tts.EDGE_VOICES[0]["uri"]
            await settings.set_edge_voice_favorite(settings.EdgeVoiceFavorite(voice=voice, favorite=True))
            self.assertEqual([v["uri"] for v in (await settings.tts_voice_list())["voices"]], [voice])
            self.assertEqual(save.call_args.args[0]["edge_tts_favorites"], [voice])
            await settings.set_edge_voice_favorite(settings.EdgeVoiceFavorite(voice=voice, favorite=False))
            self.assertEqual((await settings.tts_voice_list())["voices"], [])
            with self.assertRaises(settings.HTTPException):
                await settings.set_edge_voice_favorite(settings.EdgeVoiceFavorite(voice="speech:paid", favorite=True))

    async def test_minimax_catalog_is_independent_from_siliconflow_and_uses_configured_names(self):
        configured = {
            "minimax_aion_voice_id": "AionClone001",
            "minimax_connor_voice_id": "ConnorClone001",
            "edge_tts_favorites": ["edge:zh-CN-XiaoxiaoNeural"],
        }
        keys = {"minimax": "plan-key", "siliconflow": ""}
        with patch.object(settings, "SETTINGS", configured), \
             patch.object(settings, "get_key", side_effect=lambda provider: keys[provider]), \
             patch("chatroom.get_chatroom_names", return_value=("用户", "配置中的AI", "配置中的伙伴")):
            result = await settings.tts_voice_list()

        self.assertEqual(
            [voice["uri"] for voice in result["voices"]],
            [
                "minimax:AionClone001",
                "minimax:ConnorClone001",
                "edge:zh-CN-XiaoxiaoNeural",
            ],
        )
        self.assertEqual(result["voices"][0]["customName"], "配置中的AI · 克隆音色")
        self.assertEqual(result["voices"][1]["customName"], "配置中的伙伴 · 克隆音色")

    async def test_minimax_settings_save_without_changing_existing_tts_keys(self):
        configured = {"siliconflow_key": "keep-silicon", "edge_tts_favorites": ["edge:voice"]}
        update = settings.SettingsUpdate(
            minimax_tts_key="sk-cp-plan",
            minimax_tts_model="speech-2.6-turbo",
            minimax_aion_voice_id="AionClone001",
            minimax_connor_voice_id="ConnorClone001",
        )
        with patch.object(settings, "SETTINGS", configured), patch.object(settings, "save_settings"):
            result = await settings.update_settings(update)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(configured["siliconflow_key"], "keep-silicon")
        self.assertEqual(configured["edge_tts_favorites"], ["edge:voice"])
        self.assertEqual(configured["minimax_tts_key"], "sk-cp-plan")
        self.assertEqual(configured["minimax_tts_model"], "speech-2.6-turbo")
        self.assertEqual(configured["minimax_aion_voice_id"], "AionClone001")

    async def test_paid_voice_and_request_settings_are_preserved(self):
        with patch("tts.get_key", return_value="test"), patch.object(tts.httpx, "AsyncClient") as client:
            post = client.return_value.__aenter__.return_value.post
            post.return_value.status_code = 200
            post.return_value.content = b"paid-mp3"
            result = await tts._request_tts_audio("你好。", "speech:existing-custom-voice")
        self.assertEqual(result, b"paid-mp3")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["voice"], "speech:existing-custom-voice")
        self.assertEqual(body["gain"], 0)

    async def test_minimax_voice_uses_its_own_key_and_official_payload(self):
        keys = {"minimax": "plan-key", "siliconflow": "silicon-key"}
        with patch("tts.get_key", side_effect=lambda provider: keys[provider]), \
             patch.object(tts, "SETTINGS", {"minimax_tts_model": "speech-2.6-hd"}, create=True), \
             patch.object(tts.httpx, "AsyncClient") as client:
            post = client.return_value.__aenter__.return_value.post
            post.return_value.status_code = 200
            post.return_value.json = Mock(return_value={
                "data": {"audio": b"minimax-mp3".hex(), "status": 2},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            })
            result = await tts._request_tts_audio("你好。", "minimax:CloneVoice001")

        self.assertEqual(result, b"minimax-mp3")
        self.assertEqual(post.call_args.args[0], "https://api.minimax.cn/v1/t2a_v2")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer plan-key")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "speech-2.6-hd")
        self.assertEqual(body["voice_setting"]["voice_id"], "CloneVoice001")
        self.assertEqual(body["output_format"], "hex")


if __name__ == "__main__":
    unittest.main()
