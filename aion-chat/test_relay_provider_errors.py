import sys
import base64
import json
import httpx
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_providers import build_multimodal_messages, call_aipro, call_custom_openai, call_gemini, call_siliconflow
from routes import chat as chat_routes
from stream_safety import StreamActivity


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
    async def _replay_completion(self, provider, frames, *, timeout_at_end=True, use_meta=True):
        import ai_providers
        class Response(FakeStreamResponse):
            closed = False
            async def __aexit__(self, *args):
                self.closed = True
            async def aiter_lines(self):
                for frame in frames:
                    yield 'data: ' + json.dumps(frame)
                if timeout_at_end:
                    raise httpx.ReadTimeout('upstream kept the completed connection open')
        response = Response()
        meta = {}
        target = getattr(ai_providers, 'call_' + provider).__wrapped__
        cfg = {'base_url': 'https://relay.example', 'model': 'test'} if provider == 'custom_openai' else 'test'
        with patch('ai_providers.httpx.AsyncClient', new=fake_client_factory(response)):
            chunks = [chunk async for chunk in target([], cfg, meta if use_meta else None)]
        self.assertTrue(response.closed)
        return chunks, meta

    async def test_normal_stop_and_final_usage_close_completed_relay_streams(self):
        stop = {'choices': [{'delta': {'content': 'last'}, 'finish_reason': 'stop'}]}
        usage = {'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
        for provider in ('custom_openai', 'aipro', 'siliconflow'):
            for frames in ([{**stop, **usage}], [stop, usage]):
                with self.subTest(provider=provider, frames=len(frames)):
                    try:
                        chunks, meta = await self._replay_completion(provider, frames)
                    except httpx.ReadTimeout:
                        self.fail(f'{provider} waited after normal stop and final usage')
                    self.assertEqual(''.join(chunks), 'last')
                    self.assertEqual(meta['total_tokens'], 7)

    async def test_intermediate_usage_and_incomplete_stream_still_time_out(self):
        for provider in ('custom_openai', 'aipro', 'siliconflow'):
            with self.subTest(provider=provider), self.assertRaises(httpx.ReadTimeout):
                await self._replay_completion(provider, [
                    {'usage': {'total_tokens': 1}},
                    {'choices': [{'delta': {'content': 'partial'}, 'finish_reason': 'stop'}]},
                ])

    async def test_reasoning_only_frames_remain_activity_without_meta_requirement(self):
        for provider in ('custom_openai', 'aipro', 'siliconflow', 'gemini'):
            with self.subTest(provider=provider):
                frame = ({'candidates': [{'content': {'parts': [{'thought': True, 'text': 'thinking'}]}}]}
                         if provider == 'gemini' else {'choices': [{'delta': {'reasoning_content': 'thinking'}}]})
                chunks, _ = await self._replay_completion(provider, [frame], timeout_at_end=False, use_meta=False)
                self.assertTrue(any(isinstance(c, StreamActivity) for c in chunks))
                self.assertNotIn('thinking', ''.join(chunks))

    async def test_gemini_stop_keeps_last_text_and_usage_without_waiting_for_eof(self):
        try:
            chunks, meta = await self._replay_completion('gemini', [{
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'last'}]}}],
                'usageMetadata': {'totalTokenCount': 7},
            }])
        except httpx.ReadTimeout:
            self.fail('Gemini waited after STOP and usageMetadata')
        self.assertEqual(''.join(chunks), 'last')
        self.assertEqual(meta['total_tokens'], 7)

    async def test_vertex_stream_uses_project_endpoint_and_refreshes_only_when_needed(self):
        from unittest.mock import Mock
        from ai_providers import call_vertex

        credentials = Mock(project_id="test-project", token="test-token", valid=False)
        credentials.refresh.side_effect = lambda request: setattr(credentials, "valid", True)
        factory = fake_client_factory(FakeStreamResponse(lines=[
            'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]},"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":8}}',
        ]))
        with (
            patch("ai_providers._vertex_credentials", return_value=credentials),
            patch("ai_providers.httpx.AsyncClient", new=factory),
        ):
            for model in ("gemini-3.8-flash", "gemini-4-argon"):
                meta = {}
                chunks = [chunk async for chunk in call_vertex.__wrapped__(
                    [{"role": "user", "content": "hello"}], model, meta)]
                self.assertEqual(chunks, ["OK"])
                self.assertEqual(meta["total_tokens"], 8)
                args, kwargs = factory.clients[-1].calls[0]
                self.assertEqual(args[1], "https://aiplatform.googleapis.com/v1/projects/test-project/"
                                 f"locations/global/publishers/google/models/{model}:streamGenerateContent?alt=sse")
                self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-token"})
        credentials.refresh.assert_called_once()

    async def test_custom_openai_uses_requested_wait(self):
        response = FakeStreamResponse(lines=['data: [DONE]'])
        seen = []
        def factory(*args, **kwargs):
            seen.append(kwargs)
            return FakeAsyncClient(response)
        with patch('ai_providers.httpx.AsyncClient', new=factory):
            chunks = [chunk async for chunk in call_custom_openai(
                [{'role': 'user', 'content': 'hello'}],
                {'base_url': 'https://relay.example/v1', 'model': 'unit-model'},
                request_timeout=300,
            )]
        self.assertEqual(chunks, [])
        self.assertEqual(seen[0]['timeout'], 300)

    async def test_reasoning_reports_progress_before_visible_reply(self):
        from ai_providers import CLI_STATUS_PREFIX

        response = FakeStreamResponse(lines=[
            'data: {"choices":[{"delta":{"reasoning_content":"分析"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"上下文"}}]}',
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            'data: [DONE]',
        ])
        meta = {}
        with patch("ai_providers.httpx.AsyncClient", new=fake_client_factory(response)):
            chunks = [chunk async for chunk in call_custom_openai(
                [{"role": "user", "content": "你好"}],
                {"base_url": "https://relay.example/v1", "model": "unit-model"},
                meta,
            )]

        self.assertTrue(chunks[0].startswith(CLI_STATUS_PREFIX))
        self.assertEqual(sum(isinstance(chunk, StreamActivity) for chunk in chunks), 2)
        self.assertEqual(meta["reasoning_content"], "分析上下文")
        self.assertEqual(chunks[-1], "你好")

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
            "max_tokens": 123,
        })

    async def test_official_token_limit_and_default_temperature_preserve_relay_compatibility(self):
        for base_url, use_default, effort, token_field in (
            ("https://api.openai.com/v1", True, "high", "max_completion_tokens"),
            ("https://api.openai.com/v1", False, "none", "max_completion_tokens"),
            ("https://relay.example/v1", True, "high", "max_tokens"),
        ):
            with self.subTest(base_url=base_url, effort=effort):
                response = FakeStreamResponse(lines=["data: [DONE]"])
                factory = fake_client_factory(response)
                cfg = {
                    "base_url": base_url,
                    "model": "gpt-6.1-terra",
                    "use_default_reasoning_effort": use_default,
                    "reasoning_effort": effort,
                }
                with patch("ai_providers.httpx.AsyncClient", new=factory):
                    async for _ in call_custom_openai.__wrapped__(
                        [{"role": "user", "content": "hello"}], cfg, temperature=0.7, max_tokens=256
                    ):
                        pass
                payload = factory.clients[0].calls[0][1]["json"]
                self.assertNotIn("temperature", payload)
                self.assertEqual(payload.get(token_field), 256)
                self.assertFalse("max_tokens" in payload and "max_completion_tokens" in payload)

    async def test_chat_providers_ignore_legacy_temperature_and_keep_output_limit(self):
        for provider in (call_siliconflow, call_aipro, call_gemini):
            with self.subTest(provider=provider.__name__):
                factory = fake_client_factory(FakeStreamResponse(lines=["data: [DONE]"]))
                with patch("ai_providers.httpx.AsyncClient", new=factory):
                    async for _ in provider.__wrapped__(
                        [{"role": "user", "content": "hello"}], "unit-model",
                        temperature=0.7, max_tokens=256
                    ):
                        pass
                payload = factory.clients[0].calls[0][1]["json"]
                params = payload.get("generationConfig", payload)
                self.assertNotIn("temperature", params)
                self.assertEqual(params.get("maxOutputTokens", params.get("max_tokens")), 256)

    async def test_custom_openai_sends_selected_reasoning_effort_only_when_overridden(self):
        response = FakeStreamResponse(status_code=200, lines=["data: [DONE]"])
        cfg = {
            "base_url": "https://relay.example/v1",
            "api_key": "test-key",
            "model": "unit-model",
            "use_default_reasoning_effort": False,
            "reasoning_effort": "max",
        }
        factory = fake_client_factory(response)

        with patch("ai_providers.httpx.AsyncClient", new=factory):
            chunks = [
                chunk
                async for chunk in call_custom_openai(
                    [{"role": "user", "content": "hello"}],
                    cfg,
                )
            ]

        self.assertEqual(chunks, [])
        payload = factory.clients[0].calls[0][1]["json"]
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertNotIn("thinking", payload)

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
