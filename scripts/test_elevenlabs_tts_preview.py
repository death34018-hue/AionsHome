"""Small offline checks; no ElevenLabs credentials or paid requests."""
import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import elevenlabs_tts_preview as preview


def response(data):
    result = io.BytesIO(json.dumps(data).encode())
    result.headers = Message()
    result.headers['Content-Type'] = 'application/json'
    return result


class PreviewTests(unittest.TestCase):
    def test_second_server_cannot_share_the_preview_port(self):
        with preview.PreviewServer(("127.0.0.1", 0), preview.PreviewHandler) as first:
            with self.assertRaises(OSError):
                with preview.PreviewServer(first.server_address, preview.PreviewHandler):
                    pass

    def test_audio_tags_reach_provider_unchanged(self):
        key, voice, payload = preview.build_payload({
            "api_key": " test-key ", "voice_id": "Voice123", "model_id": "eleven_v3",
            "text": "[softly] 今天辛苦了。[chuckles] 来，抱一下。",
        })
        self.assertEqual(key, "test-key")
        self.assertEqual(voice, "Voice123")
        self.assertEqual(payload, {
            "model_id": "eleven_v3", "text": "[softly] 今天辛苦了。[chuckles] 来，抱一下。",
        })
        with self.assertRaises(preview.InputError):
            preview.build_payload({"api_key": "key", "voice_id": "Voice123",
                                   "model_id": "eleven_flash_v2_5", "text": "[softly] 你好"})

    def test_voice_list_includes_later_pages_and_deduplicates(self):
        pages = [
            {"voices": [{"voice_id": "a", "name": "克隆 A", "category": "cloned"}],
             "has_more": True, "next_page_token": "next/page+2"},
            {"voices": [{"voice_id": "a", "name": "克隆 A"},
                        {"voice_id": "b", "name": "克隆 B", "fine_tuning": {"state": {"eleven_v3": "fine_tuned"}}}],
             "has_more": False},
        ]
        urls = []
        def upstream(req, **kwargs):
            urls.append(req.full_url)
            return response(pages.pop(0))
        with patch.object(preview.urllib.request, "urlopen", side_effect=upstream):
            voices = preview.list_voices("test-key")
        self.assertEqual([v["voice_id"] for v in voices], ["a", "b"])
        self.assertEqual(voices[1]["fine_tuning"]["state"]["eleven_v3"], "fine_tuned")
        self.assertIn("next_page_token=next%2Fpage%2B2", urls[1])

    def test_incomplete_pagination_is_not_reported_as_all_voices(self):
        with patch.object(preview.urllib.request, "urlopen", return_value=response({
            "voices": [{"voice_id": "a"}], "has_more": True, "next_page_token": None,
        })):
            with self.assertRaisesRegex(RuntimeError, "分页"):
                preview.list_voices("test-key")

    def test_upstream_error_never_echoes_key(self):
        error = urllib.error.HTTPError("https://api.elevenlabs.io/v2/voices", 401, "", {},
            response({"detail": {"status": "invalid_api_key", "message": "bad secret-test-key"}}))
        with patch.object(preview.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                preview.list_voices("secret-test-key")
        self.assertNotIn("secret-test-key", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def test_list_network_errors_do_not_warn_about_generation_charges(self):
        blocked = OSError("network access denied")
        blocked.winerror = 10013
        with patch.object(preview.urllib.request, "urlopen", side_effect=urllib.error.URLError(blocked)):
            with self.assertRaises(RuntimeError) as caught:
                preview.list_voices("test-key")
        self.assertIn("网络权限", str(caught.exception))
        self.assertNotIn("扣费", str(caught.exception))
        with patch.object(preview.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError) as caught:
                preview.request_api("/v1/text-to-speech/fixture", "test-key", {"text": "你好"})
        self.assertIn("扣费", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
