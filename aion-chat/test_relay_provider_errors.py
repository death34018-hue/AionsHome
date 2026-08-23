import sys
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_providers import build_multimodal_messages, call_aipro, call_custom_openai
from routes import chat as chat_routes


class FakeStreamResponse:
    def __init__(self, status_code=200, body=b"", lines=None):
        self.status_code = status_code
        self._body = body
        self._lines = lines or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def fake_client_factory(response):
    clients = []

    def _factory(*args, **kwargs):
        client = FakeAsyncClient(response)
        clients.append(client)
        return client

    _factory.clients = clients
    return _factory


class RelayProviderErrorPassthroughTests(unittest.IsolatedAsyncioTestCase):
    def test_multimodal_messages_ignore_video_when_model_does_not_support_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "clip.mp4"
            video_path.write_bytes(b"unit video bytes")

            with patch("ai_providers._resolve_attachment_path", return_value=video_path):
                messages = build_multimodal_messages([{
                    "role": "user",
                    "content": "please inspect",
                    "attachments": ["/uploads/clip.mp4"],
                }])

        self.assertEqual(messages, [{"role": "user", "content": "please inspect"}])

    async def test_custom_openai_includes_video_when_model_supports_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "clip.mp4"
            video_path.write_bytes(b"unit video bytes")
            response = FakeStreamResponse(status_code=200, lines=["data: [DONE]"])
            factory = fake_client_factory(response)
            cfg = {
                "base_url": "https://relay.example/v1",
                "api_key": "test-key",
                "model": "video-model",
                "route_name": "Unit Relay",
                "video": True,
            }

            with (
                patch("ai_providers._resolve_attachment_path", return_value=video_path),
                patch("ai_providers.httpx.AsyncClient", new=factory),
            ):
                chunks = [
                    chunk
                    async for chunk in call_custom_openai(
                        [{
                            "role": "user",
                            "content": "please inspect",
                            "attachments": ["/uploads/clip.mp4"],
                        }],
                        cfg,
                    )
                ]

        self.assertEqual(chunks, [])
        payload = factory.clients[0].calls[0][1]["json"]
        self.assertEqual(payload["messages"][0]["content"], [
            {"type": "text", "text": "please inspect"},
            {
                "type": "video_url",
                "video_url": {
                    "url": "data:video/mp4;base64,dW5pdCB2aWRlbyBieXRlcw==",
                },
            },
        ])

    async def test_custom_openai_sends_standard_chat_completions_payload(self):
        response = FakeStreamResponse(status_code=200, lines=["data: [DONE]"])
        cfg = {
            "base_url": "https://relay.example/v1",
            "api_key": "test-key",
            "model": "unit-model",
            "route_name": "Unit Relay",
        }
        factory = fake_client_factory(response)

        with patch("ai_providers.httpx.AsyncClient", new=factory):
            chunks = [
                chunk
                async for chunk in call_custom_openai(
                    [{"role": "user", "content": "hello"}],
                    cfg,
                    temperature=0.7,
                    max_tokens=123,
                )
            ]

        self.assertEqual(chunks, [])
        args, kwargs = factory.clients[0].calls[0]
        self.assertEqual(args[:2], ("POST", "https://relay.example/v1/chat/completions"))
        self.assertEqual(kwargs["headers"], {
            "Content-Type": "application/json",
            "Authorization": "Bearer test-key",
        })
        self.assertEqual(kwargs["json"], {
            "model": "unit-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 123,
        })

    async def test_custom_openai_audio_capability_emits_input_audio(self):
        response = FakeStreamResponse(status_code=200, lines=["data: [DONE]"])
        cfg = {
            "base_url": "https://relay.example/v1",
            "api_key": "test-key",
            "model": "unit-model",
            "audio": True,
        }
        factory = fake_client_factory(response)

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_bytes = b"RIFF-provider-audio"
            audio_path.write_bytes(audio_bytes)
            with (
                patch("ai_providers._resolve_attachment_path", return_value=audio_path),
                patch("ai_providers.httpx.AsyncClient", new=factory),
            ):
                chunks = [
                    chunk
                    async for chunk in call_custom_openai(
                        [{"role": "user", "content": "transcript", "attachments": ["/uploads/voice.wav"]}],
                        cfg,
                    )
                ]

        self.assertEqual(chunks, [])
        payload = factory.clients[0].calls[0][1]["json"]
        audio_part = next(
            part for part in payload["messages"][0]["content"]
            if part.get("type") == "input_audio"
        )
        self.assertEqual(base64.b64decode(audio_part["input_audio"]["data"]), audio_bytes)

    async def test_builtin_aipro_accepts_sse_data_without_space(self):
        response = FakeStreamResponse(
            status_code=200,
            lines=[
                'data:{"choices":[{"delta":{"content":"clean"}}]}',
                "data:[DONE]",
            ],
        )

        with (
            patch("ai_providers.get_key", return_value="test-key"),
            patch("ai_providers.httpx.AsyncClient", new=fake_client_factory(response)),
        ):
            chunks = [
                chunk
                async for chunk in call_aipro(
                    [{"role": "user", "content": "hello"}],
                    "unit-model",
                )
            ]

        self.assertEqual(chunks, ["clean"])

    async def test_custom_openai_http_error_yields_raw_response_body(self):
        raw = '{"error":{"message":"upstream _provider failure"},"trace_id":"abc123"}'
        response = FakeStreamResponse(status_code=429, body=raw.encode("utf-8"))
        cfg = {
            "base_url": "https://relay.example/v1",
            "api_key": "test-key",
            "model": "unit-model",
            "route_name": "Unit Relay",
        }

        with patch("ai_providers.httpx.AsyncClient", new=fake_client_factory(response)):
            chunks = [
                chunk
                async for chunk in call_custom_openai(
                    [{"role": "user", "content": "hello"}],
                    cfg,
                )
            ]

        self.assertEqual(chunks, [raw])

    async def test_custom_openai_stream_error_yields_raw_data_payload(self):
        raw = '{"error":{"message":"stream rejected"},"type":"invalid_request_error"}'
        response = FakeStreamResponse(
            status_code=200,
            lines=[f"data: {raw}", "data: [DONE]"],
        )
        cfg = {
            "base_url": "https://relay.example/v1",
            "api_key": "test-key",
            "model": "unit-model",
            "route_name": "Unit Relay",
        }

        with patch("ai_providers.httpx.AsyncClient", new=fake_client_factory(response)):
            chunks = [
                chunk
                async for chunk in call_custom_openai(
                    [{"role": "user", "content": "hello"}],
                    cfg,
                )
            ]

        self.assertEqual(chunks, [raw])

    async def test_builtin_aipro_http_error_yields_raw_response_body(self):
        raw = '{"error":{"message":"aipro raw failure"},"trace_id":"relay-456"}'
        response = FakeStreamResponse(status_code=500, body=raw.encode("utf-8"))

        with (
            patch("ai_providers.get_key", return_value="test-key"),
            patch("ai_providers.httpx.AsyncClient", new=fake_client_factory(response)),
        ):
            chunks = [
                chunk
                async for chunk in call_aipro(
                    [{"role": "user", "content": "hello"}],
                    "unit-model",
                )
            ]

        self.assertEqual(chunks, [raw])


class ChatProviderStreamEventTests(unittest.TestCase):
    def test_custom_openai_uses_plain_chunk_event_from_model_provider(self):
        with patch.dict(
            chat_routes.MODELS,
            {"unit-custom": {"provider": "custom_openai"}},
            clear=False,
        ):
            event = chat_routes._chat_stream_event(
                "unit-custom",
                "visible text",
                "visible text",
            )

        self.assertEqual(event, {"type": "chunk", "content": "visible text"})

    def test_raw_relay_json_error_is_classified_as_error_text(self):
        self.assertTrue(
            chat_routes._is_ai_error_text(
                '{"error":{"message":"upstream rejected request"},"trace_id":"abc123"}'
            )
        )


if __name__ == "__main__":
    unittest.main()
