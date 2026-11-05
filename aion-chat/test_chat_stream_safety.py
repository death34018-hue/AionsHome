import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import aiosqlite
import routes.chat as chat_routes
from stream_safety import StreamSafetyResult

from routes.chat import _consume_chat_realtime_stream, _consume_chat_stream
from stream_safety import StreamActivity


class _Chunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _TTSProbe:
    def __init__(self):
        self.parts = []

    async def feed_async(self, text):
        self.parts.append(text)


class ChatStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_route_wrappers_forward_status_as_activity(self):
        import ast
        from pathlib import Path
        from types import SimpleNamespace
        from ai_providers import CLI_STATUS_PREFIX

        async def source(*args, **kwargs):
            yield StreamActivity()
            yield CLI_STATUS_PREFIX + 'progress'
            yield 'reply'

        checked = 0
        for filename in ('chat.py', 'chatroom.py'):
            tree = ast.parse((Path(__file__).parent / 'routes' / filename).read_text(encoding='utf-8'))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef) or fn.name != 'content_stream':
                    continue
                queue = asyncio.Queue()
                env = {'stream_ai': source, '_stream_connor_model': source, 'kitchen_stream': chat_routes.kitchen_stream,
                       'model_started': lambda: lambda c: None, 'kitchen_state': {}, 'conv_id': 'conv', 'room_id': 'room',
                       'query_text': '', 'lounge_request_text': '',
                       'CLI_STATUS_PREFIX': CLI_STATUS_PREFIX, 'StreamActivity': StreamActivity,
                       '_q': queue, 'history': [], 'model_key': 'test', 'usage_meta': {},
                       'body': SimpleNamespace(max_tokens=None, content=''), 'cancel_event': None,
                       'temperature': None, 'max_tokens': None, 'connor_messages': [],
                       'connor_history': [], 'aion_history': [], 'connor_model_key': 'test'}
                factory = ast.parse('def factory():\n    has_reply = False\n    pass\n    return content_stream\n')
                factory.body[0].body[1] = fn
                ast.fix_missing_locations(factory)
                exec(compile(factory, filename, 'exec'), env)
                with patch('kitchen_chat.kitchen.enabled', return_value=False):
                    chunks = [c async for c in env['factory']()()]
                with self.subTest(filename=filename, line=fn.lineno):
                    self.assertEqual(sum(isinstance(c, StreamActivity) for c in chunks), 2)
                    self.assertEqual(''.join(chunks), 'reply')
                    self.assertEqual(queue.qsize(), 1)
                checked += 1
        self.assertEqual(checked, 6)

    async def test_stopped_reply_is_saved_with_checked_prefix_and_reason(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE conversations (id TEXT PRIMARY KEY, updated_at REAL);
                CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT,
                                       content TEXT, created_at REAL);
                INSERT INTO conversations VALUES ('conv', 0);
            """)
            @asynccontextmanager
            async def get_db():
                yield db

            queue = asyncio.Queue()
            with patch.object(chat_routes, "get_db", get_db), \
                    patch.object(chat_routes.manager, "broadcast", new=AsyncMock()):
                await chat_routes._save_stopped_chat_reply(
                    "conv", "reply", "正常的前半段。", StreamSafetyResult(
                        "", stop_reason="repetition", notice="检测到持续重复，已停止生成。"), queue)
            saved = await (await db.execute("SELECT content FROM messages WHERE id='reply'")).fetchone()
            self.assertEqual(saved[0], "正常的前半段。\n\n[检测到持续重复，已停止生成。]")
            event = queue.get_nowait()
            self.assertEqual(event["type"], "stream_error")
            self.assertEqual(event["message"]["content"], saved[0])

    async def test_safe_live_failure_keeps_checked_text_without_retry(self):
        queue = asyncio.Queue()
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Chunks(["安全正文。" * 8, "�" * 8])
            return _Chunks(["旧管线完整回复"])

        result, visible_text, used_fallback, manual_retry = await _consume_chat_realtime_stream(
            source_factory,
            queue,
            model_key="Codex-Sol",
            transport_mode="safe_live",
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(attempts, 1)
        self.assertFalse(used_fallback)
        self.assertTrue(manual_retry)
        self.assertTrue(result.committed_text.startswith("安全正文。"))
        self.assertEqual(visible_text, result.committed_text)
        self.assertNotIn({"type": "stream_reset"}, events)

    async def test_provider_activity_is_not_visible_or_sent_to_tts(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([StreamActivity(), "真实正文"])

        result, visible_text = await _consume_chat_stream(
            source,
            queue,
            model_key="test-model",
            tts_streamer=tts,
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(result.committed_text, "真实正文")
        self.assertEqual(visible_text, "真实正文")
        self.assertEqual("".join(tts.parts), "真实正文")
        self.assertEqual(
            "".join(event.get("content", "") for event in events),
            "真实正文",
        )

    async def test_private_chat_tts_receives_only_validated_text(self):
        queue = asyncio.Queue()
        tts = _TTSProbe()
        source = _Chunks([
            "Safe English and 中文 😏。" * 100,
            "\x01" * 20,
            "never delivered",
        ])

        result, visible_text = await _consume_chat_stream(
            source,
            queue,
            model_key="test-model",
            tts_streamer=tts,
        )

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertEqual("".join(tts.parts), result.committed_text)
        self.assertNotIn("\x01", visible_text)
        self.assertNotIn("never delivered", visible_text)
        self.assertNotIn(result.notice, "".join(tts.parts))


if __name__ == "__main__":
    unittest.main()
