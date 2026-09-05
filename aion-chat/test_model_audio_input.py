import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent

from config import normalize_custom_model_routes
from ai_providers import (
    build_gemini_contents,
    build_multimodal_messages,
    filter_audio_attachments_for_model,
)
from routes.chat import _process_voice_attachments_in_history
from routes.chatroom import _process_voice_attachments


def voice_attachment(url="/uploads/voice.wav", transcript="暗号内容"):
    return {
        "type": "voice",
        "url": url,
        "duration": 2.5,
        "transcript": transcript,
    }


class ModelAudioCapabilityConfigTests(unittest.TestCase):
    def test_missing_audio_capability_defaults_to_false(self):
        routes = normalize_custom_model_routes([
            {
                "id": "route-1",
                "name": "test",
                "base_url": "https://relay.example/v1",
                "models": [{"key": "legacy", "model": "legacy-model", "vision": True}],
            }
        ])

        self.assertFalse(routes[0]["models"][0]["audio"])

    def test_explicit_audio_capability_survives_normalization(self):
        routes = normalize_custom_model_routes([
            {
                "id": "route-1",
                "name": "test",
                "base_url": "https://relay.example/v1",
                "models": [{"key": "listener", "model": "gemini-model", "audio": True}],
            }
        ])

        self.assertTrue(routes[0]["models"][0]["audio"])

    def test_non_boolean_audio_value_cannot_enable_original_audio(self):
        routes = normalize_custom_model_routes([
            {
                "id": "route-1",
                "name": "test",
                "base_url": "https://relay.example/v1",
                "models": [{"key": "unsafe", "model": "unit-model", "audio": "false"}],
            }
        ])

        self.assertFalse(routes[0]["models"][0]["audio"])

    def test_settings_ui_has_unchecked_audio_toggle_and_persists_field(self):
        source = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")

        self.assertIn("可听音频", source)
        self.assertIn("audio: false", source)
        self.assertIn("model.audio === true ? \"checked\" : \"\"", source)
        self.assertIn("audio: model.audio === true", source)


class ModelVideoCapabilityConfigTests(unittest.TestCase):
    def test_video_capability_defaults_off_and_preserves_explicit_enablement(self):
        routes = normalize_custom_model_routes([{
            "id": "route-1",
            "name": "test",
            "base_url": "https://relay.example/v1",
            "models": [
                {"key": "legacy", "model": "legacy-model"},
                {"key": "viewer", "model": "video-model", "video": True},
            ],
        }])

        self.assertEqual(
            [model.get("video") for model in routes[0]["models"]],
            [False, True],
        )


class ModelReasoningEffortConfigTests(unittest.TestCase):
    def test_reasoning_effort_defaults_to_api_and_migrates_medium_to_high(self):
        routes = normalize_custom_model_routes([{
            "id": "route-1",
            "name": "test",
            "base_url": "https://relay.example/v1",
            "models": [
                {"key": "legacy", "model": "legacy-model"},
                {
                    "key": "balanced",
                    "model": "balanced-model",
                    "use_default_reasoning_effort": False,
                    "reasoning_effort": "medium",
                },
            ],
        }])

        models = routes[0]["models"]
        self.assertEqual(
            [(model["use_default_reasoning_effort"], model["reasoning_effort"]) for model in models],
            [(True, "high"), (False, "high")],
        )


class LatestUserVoiceRetentionTests(unittest.TestCase):
    def test_selected_image_survives_later_ai_transcript_entry(self):
        for processor in (
            _process_voice_attachments_in_history,
            _process_voice_attachments,
        ):
            with self.subTest(processor=processor.__name__):
                history = [
                    {
                        "role": "user",
                        "content": "当前用户消息 - 用户：看照片",
                        "attachments": ["/uploads/photo.png"],
                    },
                    {
                        "role": "user",
                        "content": "历史消息 - AI：我看到了",
                    },
                ]

                processor(history)

                self.assertEqual(
                    history[0].get("attachments"),
                    ["/uploads/photo.png"],
                )

    def test_private_history_keeps_voice_on_latest_user_even_with_assistant_after_it(self):
        history = [
            {"role": "user", "content": "", "attachments": [voice_attachment()]},
            {"role": "assistant", "content": "回答", "attachments": []},
        ]

        _process_voice_attachments_in_history(history)

        self.assertEqual(history[0]["attachments"], ["/uploads/voice.wav"])
        self.assertIn("[语音消息] 暗号内容", history[0]["content"])

    def test_group_history_does_not_resend_old_voice_after_newer_user_text(self):
        history = [
            {"role": "user", "content": "", "attachments": [voice_attachment()]},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "新的文字", "attachments": []},
        ]

        _process_voice_attachments(history)

        self.assertNotIn("attachments", history[0])
        self.assertIn("[语音消息] 暗号内容", history[0]["content"])
        self.assertEqual(history[2]["content"], "新的文字")

    def test_group_history_keeps_voice_when_latest_user_message_is_voice(self):
        history = [
            {"role": "user", "content": "旧消息", "attachments": []},
            {"role": "user", "content": "", "attachments": [voice_attachment()]},
            {"role": "assistant", "content": "第一位 AI 的回答"},
        ]

        _process_voice_attachments(history)

        self.assertEqual(history[1]["attachments"], ["/uploads/voice.wav"])

    def test_ambient_context_is_appended_before_final_voice_pruning(self):
        source = (ROOT / "routes" / "chatroom.py").read_text(encoding="utf-8")
        aion_block = source[source.index("async def _reply_aion"):source.index("async def _reply_connor")]
        connor_block = source[source.index("async def _reply_connor"):source.index("@router.post(\"/rooms/{room_id}/ai-chat\")")]

        for block in (aion_block, connor_block):
            self.assertLess(block.index("if ambient_context:"), block.index("_process_voice_attachments("))


class ProviderAudioSerializationTests(unittest.TestCase):
    def _history(self):
        return [{
            "role": "user",
            "content": "[语音消息] 暗号内容",
            "attachments": ["/uploads/voice.wav"],
        }]

    def test_openai_audio_enabled_emits_real_input_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_bytes = b"RIFF-unit-audio"
            audio_path.write_bytes(audio_bytes)

            with patch("ai_providers._resolve_attachment_path", return_value=audio_path):
                messages = build_multimodal_messages(self._history(), include_audio=True)

        parts = messages[0]["content"]
        audio_part = next(part for part in parts if part["type"] == "input_audio")
        self.assertEqual(audio_part["input_audio"]["format"], "wav")
        self.assertEqual(base64.b64decode(audio_part["input_audio"]["data"]), audio_bytes)

    def test_openai_audio_disabled_sends_transcript_only_without_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"RIFF-unit-audio")

            with patch("ai_providers._resolve_attachment_path", return_value=audio_path):
                messages = build_multimodal_messages(self._history(), include_audio=False)

        self.assertEqual(messages, [{"role": "user", "content": "[语音消息] 暗号内容"}])

    def test_gemini_audio_is_inline_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"RIFF-unit-audio")

            with patch("ai_providers._resolve_attachment_path", return_value=audio_path):
                disabled = build_gemini_contents(self._history(), include_audio=False)
                enabled = build_gemini_contents(self._history(), include_audio=True)

        self.assertFalse(any("inline_data" in part for part in disabled[0]["parts"]))
        self.assertTrue(any("inline_data" in part for part in enabled[0]["parts"]))

    def test_disabled_model_removes_audio_before_any_provider_dispatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            image_path = Path(tmpdir) / "photo.png"
            audio_path.write_bytes(b"RIFF-unit-audio")
            image_path.write_bytes(b"unit-image")

            def resolve(att):
                return audio_path if "voice" in str(att) else image_path

            history = [{
                "role": "user",
                "content": "transcript",
                "attachments": ["/uploads/voice.wav", "/uploads/photo.png"],
            }]
            with patch("ai_providers._resolve_attachment_path", side_effect=resolve):
                filtered = filter_audio_attachments_for_model(history, include_audio=False)

        self.assertEqual(filtered[0]["attachments"], ["/uploads/photo.png"])
        self.assertEqual(history[0]["attachments"], ["/uploads/voice.wav", "/uploads/photo.png"])


if __name__ == "__main__":
    unittest.main()
