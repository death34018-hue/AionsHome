import sys
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
    def test_multimodal_messages_do_not_emit_non_standard_video_parts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "clip.mp4"
            video_path.write_bytes(b"unit video bytes")

            with patch("ai_providers._resolve_attachment_path", return_value=video_path):
                messages = build_multimodal_messages([
                    {
                        "role": "user",
                        "content": "please inspect",
                        "attachments": ["/uploads/clip.mp4"],
                    }
                ])

        parts = messages[0]["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[0], {"type": "text", "text": "please inspect"})
        self.assertTrue(all(part.get("type") in {"text", "image_url"} for part in parts))
        self.assertNotIn("video_url", {part.get("type") for part in parts})
        self.assertIn("clip.mp4", parts[1]["text"])
        self.assertIn("video/mp4", parts[1]["text"])

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
