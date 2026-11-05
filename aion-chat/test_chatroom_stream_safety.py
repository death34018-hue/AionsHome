import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import MagicMock
import aiosqlite
import routes.chatroom as chatroom_routes

from routes.chatroom import (
    _chatroom_reply_failure_text,
    _consume_chatroom_realtime_stream,
    _consume_chatroom_stream,
    _save_chatroom_generation_failure,
    _save_chatroom_reply_failure,
)


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


class ChatroomStreamSafetyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Transport tests must not depend on the user's live kitchen switch or database.
        self.enterContext(patch('kitchen.enabled', return_value=False))

    async def test_group_whispers_are_spoken_in_place_with_tts_switch(self):
        for sender, mode, enabled, reply, spoken in (
            ("aion", "safe_live", True, "前文。[悄悄话：只对你说。]后文。", "前文。只对你说。后文。"),
            ("connor", "legacy", True, "前文。[悄悄话:只对你说。]后文。", "前文。只对你说。后文。"),
            ("connor", "safe_live", True, "[悄悄话：只对你说。]", "只对你说。"),
            ("aion", "legacy", False, "[悄悄话：只对你说。]", ""),
        ):
            with self.subTest(sender=sender, mode=mode, enabled=enabled), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                queue = asyncio.Queue()
                real_streamer = chatroom_routes.TTSStreamer
                audio_texts = []

                async def stream(*args):
                    for char in reply:
                        yield char

                async def audio(text, *args, **kwargs):
                    audio_texts.append(text)
                    return b"audio"

                def make_streamer(msg_id, voice, manager, **kwargs):
                    return real_streamer(msg_id, voice, cache_dir=Path(tmp), cache_max_bytes=None, **kwargs)

                async def save(room_id, sender, content, msg_id, **kwargs):
                    self.assertFalse(kwargs["auto_tts"], "group playback must follow the request switch")
                    return {"id": msg_id, "sender": sender, "content": content, "attachments": kwargs.get("attachments", [])}

                for name, value in {
                    "build_aion_group_context": AsyncMock(return_value=([], {})),
                    "build_connor_group_context": AsyncMock(return_value=([], {})),
                    "stream_ai": stream, "_stream_connor_model": stream,
                    "resolve_model_transport_mode": lambda _: mode,
                    "TTSStreamer": make_streamer, "_save_msg": save,
                    "_get_room_type": AsyncMock(return_value="group"),
                    "_send_private_whisper": AsyncMock(return_value={"target_type": "conversation" if sender == "aion" else "room", "target_id": "private-target"}),
                    "_extract_and_save_images": AsyncMock(return_value=[]),
                    "_emit_chatroom_debug": AsyncMock(),
                    "_chatroom_debug_payload": MagicMock(return_value={}),
                    "_fire_chatroom_followups": MagicMock(),
                }.items():
                    stack.enter_context(patch.object(chatroom_routes, name, value))
                stack.enter_context(patch("tts._request_tts_audio", new=audio))
                if sender == "aion":
                    await chatroom_routes._reply_aion("room", [], 30, "hello", "model", queue, tts_enabled=enabled, tts_voice="voice")
                else:
                    await chatroom_routes._reply_connor("room", [], 30, "hello", queue, tts_enabled=enabled, tts_voice="voice")
                self.assertEqual("".join(audio_texts), spoken)
                events = [queue.get_nowait() for _ in range(queue.qsize())]
                message = next(e["message"] for e in events if e["type"] == f"{sender}_done")
                self.assertNotIn("只对你说", message["content"])
                self.assertIn({"type": "private_whisper_sent", "count": 1,
                               "target_type": "conversation" if sender == "aion" else "room",
                               "target_id": "private-target"}, message["attachments"])
                from context_builder import render_merged_timeline
                history = render_merged_timeline([{**message, "source": "group", "created_at": 1}], sender)
                self.assertEqual(history, render_merged_timeline([{**message, "source": "group", "created_at": 1, "attachments": []}], sender))
                if mode == "safe_live":
                    visible = "".join(e["content"] for e in events if e["type"] == f"{sender}_chunk")
                    self.assertNotIn("只对你说", visible)

    async def test_whisper_delivery_failure_has_no_sent_receipt(self):
        with (
            patch.object(chatroom_routes, "_get_room_type", new=AsyncMock(return_value="group")),
            patch.object(chatroom_routes, "_send_private_whisper", new=AsyncMock(side_effect=RuntimeError("delivery failed"))),
        ):
            content, triggered = await chatroom_routes._process_chatroom_commands(
                "前文。[悄悄话：私聊内容]后文。", "room", "aion", "msg", asyncio.Queue())
        self.assertEqual(content, "前文。后文。")
        self.assertEqual(chatroom_routes._private_whisper_attachments(triggered), [])

    async def test_connor_whisper_delivery_does_not_start_separate_audio(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE chatroom_rooms (id TEXT, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages (
                    id TEXT, room_id TEXT, sender TEXT, content TEXT,
                    attachments TEXT, reasoning_content TEXT, created_at REAL
                );
                INSERT INTO chatroom_rooms VALUES ('private', 'connor_1v1', 0);
            """)

            @asynccontextmanager
            async def get_db():
                yield db

            with (
                patch.object(chatroom_routes, "get_db", get_db),
                patch.object(chatroom_routes, "_get_or_create_connor_private_room", new=AsyncMock(return_value="private")),
                patch.object(chatroom_routes, "broadcast_synced", new=AsyncMock()),
                patch.object(chatroom_routes, "connor_1v1_on_message"),
                patch.object(chatroom_routes, "_with_link_previews", new=AsyncMock(side_effect=lambda text, atts: atts)),
                patch.object(chatroom_routes, "with_band_vibration_attachment", new=AsyncMock(side_effect=lambda msg_id, atts: atts)),
                patch.object(chatroom_routes, "_chatroom_auto_tts_voice", return_value="voice"),
                patch.object(chatroom_routes, "synthesize_message_tts_later") as synthesize,
            ):
                target = await chatroom_routes._send_private_whisper("connor", "私聊内容")
                self.assertEqual(target, {"target_type": "room", "target_id": "private"})
                cursor = await db.execute("SELECT content FROM chatroom_messages WHERE room_id='private'")
                self.assertEqual((await cursor.fetchone())[0], "私聊内容")
                synthesize.assert_not_called()

    async def test_reply_is_finalized_while_audio_is_still_synthesizing(self):
        # A slow provider must not hold the final, formatted message hostage.
        for route in ("aion", "connor", "connor_private"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                queue = asyncio.Queue()
                release_audio = asyncio.Event()
                audio_started = asyncio.Event()
                sender = "aion" if route == "aion" else "connor"
                reply = "**这是一条已经生成完整的回复。**"
                real_streamer = chatroom_routes.TTSStreamer

                async def stream(*args):
                    yield reply

                async def slow_audio(*args, **kwargs):
                    audio_started.set()
                    await release_audio.wait()
                    return b"audio"

                def make_streamer(msg_id, voice, manager, **kwargs):
                    return real_streamer(msg_id, voice, cache_dir=Path(tmp),
                                         cache_max_bytes=None, **kwargs)

                async def save(room_id, sender, content, msg_id, **kwargs):
                    return {"id": msg_id, "sender": sender, "content": content, "attachments": []}

                for name, value in {
                    "build_aion_group_context": AsyncMock(return_value=([], {})),
                    "build_connor_group_context": AsyncMock(return_value=([], {})),
                    "build_connor_1v1_context": AsyncMock(return_value=([], {})),
                    "stream_ai": stream, "_stream_connor_model": stream,
                    "resolve_model_transport_mode": lambda _: "legacy",
                    "TTSStreamer": make_streamer, "_save_msg": save,
                    "_process_chatroom_commands": AsyncMock(return_value=(reply, {})),
                    "_extract_and_save_images": AsyncMock(return_value=[]),
                    "_emit_chatroom_debug": AsyncMock(),
                    "_chatroom_debug_payload": MagicMock(return_value={}),
                    "_fire_chatroom_followups": MagicMock(),
                }.items():
                    stack.enter_context(patch.object(chatroom_routes, name, value))
                stack.enter_context(patch("tts._request_tts_audio", new=slow_audio))

                if route == "aion":
                    coroutine = chatroom_routes._reply_aion(
                        "room", [], 30, "hello", "model", queue, tts_enabled=True, tts_voice="voice")
                elif route == "connor":
                    coroutine = chatroom_routes._reply_connor(
                        "room", [], 30, "hello", queue, tts_enabled=True, tts_voice="voice")
                else:
                    coroutine = chatroom_routes._generate_connor_reply(
                        "room", {}, [], queue, 30, tts_enabled=True, tts_connor_voice="voice")
                task = asyncio.create_task(coroutine)
                events = []
                try:
                    async def receive_final():
                        while True:
                            event = await queue.get()
                            events.append(event)
                            if event["type"] == f"{sender}_done":
                                return event

                    final = await asyncio.wait_for(receive_final(), 1)
                    self.assertEqual(final["message"]["content"], reply)
                    await asyncio.wait_for(audio_started.wait(), 1)
                    self.assertFalse(task.done(), "SSE must stay open for the remaining audio")
                    self.assertNotIn("tts_done", [event["type"] for event in events])
                finally:
                    release_audio.set()
                    await asyncio.wait_for(task, 2)
                remaining = [queue.get_nowait()["type"] for _ in range(queue.qsize())]
                self.assertIn("tts_chunk", remaining)
                self.assertIn("tts_done", remaining)

    async def test_persisted_failed_reply_can_be_loaded_and_regenerated_by_same_ai(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE chatroom_rooms (id TEXT, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages (
                    id TEXT, room_id TEXT, sender TEXT, content TEXT,
                    attachments TEXT, reasoning_content TEXT, created_at REAL
                );
                INSERT INTO chatroom_rooms VALUES ('room', 'group', 0);
                INSERT INTO chatroom_messages VALUES ('user', 'room', 'user', 'hello', '[]', '', 1);
            """)

            @asynccontextmanager
            async def get_db():
                yield db

            with (
                patch.object(chatroom_routes, "get_db", get_db),
                patch.object(chatroom_routes, "broadcast_synced", new=AsyncMock()),
                patch.object(chatroom_routes, "connor_1v1_on_message"),
                patch.object(chatroom_routes, "_with_link_previews", new=AsyncMock(side_effect=lambda text, atts: atts)),
                patch.object(chatroom_routes, "with_band_vibration_attachment", new=AsyncMock(side_effect=lambda msg_id, atts: atts)),
                patch.object(chatroom_routes, "synthesize_message_tts_later") as synthesize,
                patch.object(chatroom_routes, "_reply_aion", new=AsyncMock()) as aion,
                patch.object(chatroom_routes, "_reply_connor", new=AsyncMock()) as connor,
            ):
                await _save_chatroom_reply_failure(
                    "room", "aion", asyncio.Queue(), "idle_timeout",
                    partial_text="已收到的回复", msg_id="failed",
                )
                messages = await chatroom_routes.list_messages("room", limit=50, before=None)
                self.assertEqual(messages[-1]["sender"], "aion")
                self.assertIn("已收到的回复", messages[-1]["content"])
                self.assertEqual(messages[-1]["attachments"][0]["type"], "chatroom_reply_failure")
                synthesize.assert_not_called()
                response = await chatroom_routes.regenerate_chatroom_message.__wrapped__(
                    "failed", chatroom_routes.MsgRegenerate(),
                )
                async for _ in response.body_iterator:
                    pass
                aion.assert_awaited_once()
                connor.assert_not_awaited()
                self.assertEqual(aion.call_args.args[3], "hello")
                remaining = await chatroom_routes.list_messages("room", limit=50, before=None)
                self.assertEqual([msg["id"] for msg in remaining], ["user"])

    async def test_provider_error_is_not_a_successful_reply_or_spoken(self):
        error = '{"error":{"message":"upstream timeout","type":"server_error"}}'
        async def stream(*args):
            yield error
        queue = asyncio.Queue()
        tts = MagicMock()
        tts.flush = AsyncMock()
        save = AsyncMock(return_value={"id": "failure", "sender": "system"})
        with ExitStack() as stack:
            for name, value in {
                "build_aion_group_context": AsyncMock(return_value=([], {})),
                "_process_voice_attachments": MagicMock(),
                "stream_ai": stream,
                "resolve_model_transport_mode": MagicMock(return_value="legacy"),
                "TTSStreamer": MagicMock(return_value=tts),
                "_save_msg": save,
                "_process_chatroom_commands": AsyncMock(return_value=(error, {})),
                "_extract_and_save_images": AsyncMock(return_value=[]),
                "_emit_chatroom_debug": AsyncMock(),
                "_chatroom_debug_payload": MagicMock(return_value={}),
                "_fire_chatroom_followups": MagicMock(),
            }.items():
                stack.enter_context(patch.object(chatroom_routes, name, value))
            await chatroom_routes._reply_aion("room", [], 30, "hello", "model", queue,
                                              tts_enabled=True, tts_voice="voice")
        events = [queue.get_nowait()["type"] for _ in range(queue.qsize())]
        self.assertIn("aion_failed", events)
        self.assertNotIn("aion_done", events)
        self.assertIn("upstream timeout", save.call_args.args[2])
        self.assertEqual(save.call_args.args[1], "aion")
        self.assertFalse(save.call_args.kwargs["auto_tts"])
        tts.feed.assert_not_called()

    async def test_context_failure_reports_reason_without_stopping_other_participant(self):
        queue = asyncio.Queue()
        with (
            patch.object(chatroom_routes, "load_chatroom_config", return_value={"reply_order": "aion"}),
            patch.object(chatroom_routes, "instant_digest", new=AsyncMock(return_value={})),
            patch.object(chatroom_routes, "build_aion_group_context", new=AsyncMock(side_effect=RuntimeError("context unavailable"))),
            patch.object(chatroom_routes, "_save_msg", new=AsyncMock(return_value={"id": "failure"})) as save,
            patch.object(chatroom_routes, "_load_room_and_messages", new=AsyncMock(return_value=({}, []))),
            patch.object(chatroom_routes, "_reply_connor", new=AsyncMock()) as other,
        ):
            await chatroom_routes._generate_group_replies("room", {}, [], "model", "Codex", queue, 30)
        self.assertIn("context unavailable", save.call_args.args[2])
        other.assert_awaited_once()

    async def test_normal_reply_after_failure_still_feeds_tts(self):
        async def stream(*args):
            yield "正常回复"
        queue = asyncio.Queue()
        tts = MagicMock()
        tts.flush = AsyncMock()
        with ExitStack() as stack:
            for name, value in {
                "build_aion_group_context": AsyncMock(return_value=([], {})),
                "_process_voice_attachments": MagicMock(), "stream_ai": stream,
                "resolve_model_transport_mode": MagicMock(return_value="legacy"),
                "TTSStreamer": MagicMock(return_value=tts),
                "_save_msg": AsyncMock(return_value={"id": "normal", "sender": "aion"}),
                "_process_chatroom_commands": AsyncMock(return_value=("正常回复", {})),
                "_extract_and_save_images": AsyncMock(return_value=[]),
                "_emit_chatroom_debug": AsyncMock(),
                "_chatroom_debug_payload": MagicMock(return_value={}),
                "_fire_chatroom_followups": MagicMock(),
            }.items():
                stack.enter_context(patch.object(chatroom_routes, name, value))
            await _save_chatroom_reply_failure("room", "aion", queue, "timeout")
            await chatroom_routes._reply_aion("room", [], 30, "hello", "model", queue,
                                              tts_enabled=True, tts_voice="voice")
        tts.feed.assert_called_once_with("正常回复")
        tts.flush.assert_awaited_once()

    async def test_failure_saves_partial_text_and_reason_under_original_id(self):
        queue = asyncio.Queue()
        with patch.object(chatroom_routes, "_save_msg", new=AsyncMock(return_value={"id": "partial"})) as save:
            await _save_chatroom_reply_failure("room", "aion", queue, "idle_timeout",
                                              partial_text="已经收到的正文", msg_id="partial")
        self.assertIn("已经收到的正文", save.call_args.args[2])
        self.assertEqual(save.call_args.args[1], "aion")
        self.assertIn("超时", save.call_args.args[2])
        self.assertEqual(save.call_args.kwargs["msg_id"], "partial")
        self.assertFalse(save.call_args.kwargs["auto_tts"])

    async def test_failed_safe_fallback_keeps_received_prefix(self):
        async def stream():
            yield "已经收到的正文。" * 30
            raise TimeoutError()
        outcome = await _consume_chatroom_realtime_stream(stream, asyncio.Queue(),
            chunk_type="aion_chunk", transport_mode="safe_live")
        self.assertTrue(outcome.manual_retry_required)
        self.assertIn("已经收到的正文", outcome.result.committed_text)

    def test_failure_notice_distinguishes_timeout_and_uses_configured_name(self):
        with patch("routes.chatroom._name_for_identity", return_value="测试角色"):
            self.assertEqual(
                _chatroom_reply_failure_text("aion", "request timeout"),
                "测试角色 本次回复超时，请重试。",
            )
            self.assertEqual(
                _chatroom_reply_failure_text("aion", "connection reset"),
                "测试角色 本次回复失败：connection reset",
            )

    async def test_failure_notice_is_persisted_without_tts_and_sent_to_client(self):
        queue = asyncio.Queue()
        saved = {
            "id": "failure-1",
            "sender": "aion",
            "content": "测试角色 本次回复超时，请重试。",
        }
        with (
            patch("routes.chatroom._name_for_identity", return_value="测试角色"),
            patch("routes.chatroom._save_msg", new=AsyncMock(return_value=saved)) as save_msg,
        ):
            await _save_chatroom_reply_failure("room-1", "aion", queue, "timeout")

        save_msg.assert_awaited_once()
        self.assertEqual(save_msg.await_args.args[1], "aion")
        self.assertFalse(save_msg.await_args.kwargs["auto_tts"])
        self.assertEqual(save_msg.await_args.kwargs["attachments"][0]["type"], "chatroom_reply_failure")
        self.assertEqual(
            await queue.get(),
            {"type": "aion_failed", "content": saved["content"], "message": saved},
        )

    async def test_connor_failure_keeps_original_sender_even_without_text(self):
        with patch.object(chatroom_routes, "_save_msg", new=AsyncMock()) as save:
            await _save_chatroom_reply_failure("room", "connor", asyncio.Queue(), "empty_response")
        self.assertEqual(save.call_args.args[1], "connor")
        self.assertFalse(save.call_args.kwargs["auto_tts"])

    async def test_failed_stream_cancels_tts_and_preserves_received_text(self):
        async def stream():
            yield "已经收到的正文。" * 30
            raise RuntimeError("connection reset")

        tts = MagicMock()
        tts.feed_async = AsyncMock()
        tts.abort = AsyncMock()
        tts.has_emitted_audio = True
        outcome = await _consume_chatroom_realtime_stream(
            stream, asyncio.Queue(), chunk_type="aion_chunk",
            transport_mode="safe_live", tts_streamer=tts,
        )
        self.assertTrue(outcome.manual_retry_required)
        self.assertIn("已经收到的正文", outcome.result.committed_text)
        self.assertTrue(tts.cancel.called)

    async def test_unexpected_generation_failure_is_also_persisted_without_tts(self):
        queue = asyncio.Queue()
        saved = {"id": "failure-2", "sender": "system", "content": "本次回复失败，请重试。"}
        with patch("routes.chatroom._save_msg", new=AsyncMock(return_value=saved)) as save_msg:
            await _save_chatroom_generation_failure("room-1", queue)

        self.assertFalse(save_msg.await_args.kwargs["auto_tts"])
        self.assertEqual(
            await queue.get(),
            {"type": "error", "content": saved["content"], "message": saved},
        )

    async def test_connor_safe_live_failure_keeps_text_without_retry(self):
        queue = asyncio.Queue()
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Chunks(["安全正文。" * 8, "�" * 8])
            return _Chunks(["旧管线完整回复"])

        outcome = await _consume_chatroom_realtime_stream(
            source_factory,
            queue,
            chunk_type="connor_chunk",
            transport_mode="safe_live",
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertEqual(attempts, 1)
        self.assertFalse(outcome.used_fallback)
        self.assertTrue(outcome.manual_retry_required)
        self.assertTrue(outcome.result.committed_text.startswith("安全正文。"))
        self.assertNotIn({"type": "connor_reset"}, events)

    async def test_chatroom_emits_clean_prefix_and_nonspoken_stop_notice(self):
        queue = asyncio.Queue()
        source = _Chunks([
            "Normal English 😏 与中文正文。" * 100,
            "�" * 30,
            "unreachable",
        ])

        result = await _consume_chatroom_stream(
            source,
            queue,
            chunk_type="aion_chunk",
        )
        events = []
        while not queue.empty():
            events.append(await queue.get())
        visible = "".join(event["content"] for event in events)

        self.assertEqual(result.stop_reason, "quality")
        self.assertTrue(source.closed)
        self.assertIn("Normal English", result.committed_text)
        self.assertNotIn("�", visible)
        self.assertNotIn("unreachable", visible)
        self.assertTrue(visible.endswith(f"[{result.notice}]"))

    async def test_aion_safe_live_failure_does_not_reset_text(self):
        queue = asyncio.Queue()
        attempts = 0

        def source_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _Chunks(["临时正文。" * 8, "�" * 8])
            return _Chunks(["完整回复"])

        outcome = await _consume_chatroom_realtime_stream(
            source_factory,
            queue,
            chunk_type="aion_chunk",
            transport_mode="safe_live",
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())
        self.assertFalse(outcome.used_fallback)
        self.assertNotIn({"type": "aion_reset"}, events)


if __name__ == "__main__":
    unittest.main()
