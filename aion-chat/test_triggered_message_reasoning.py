import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schedule as schedule_module
import location as location_module


class FakeCursor:
    async def fetchone(self):
        return None

    async def fetchall(self):
        return []


class ValueCursor:
    def __init__(self, value):
        self.value = value

    async def fetchone(self):
        return self.value

    async def fetchall(self):
        return []


class RecordingDb:
    def __init__(self, statements):
        self.statements = statements
        self.row_factory = None

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return FakeCursor()

    async def commit(self):
        return None


class ConversationDb(RecordingDb):
    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "FROM conversations" in sql:
            return ValueCursor({"id": "conv_test", "model": "unit-model"})
        return FakeCursor()


class RecordingDbContext:
    def __init__(self, statements, db_cls=RecordingDb):
        self.statements = statements
        self.db_cls = db_cls

    async def __aenter__(self):
        return self.db_cls(self.statements)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TriggeredMessageReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_triggered_message_saves_and_broadcasts_reasoning(self):
        statements = []

        def fake_get_db():
            return RecordingDbContext(statements)

        broadcast = AsyncMock()
        manager = schedule_module.ScheduleManager()

        with ExitStack() as stack:
            stack.enter_context(patch("schedule.get_db", new=fake_get_db))
            stack.enter_context(patch.object(schedule_module.manager, "broadcast", new=broadcast))
            stack.enter_context(patch("routes.files.export_conversation", new=AsyncMock()))

            await manager._save_to_private(
                "conv_test",
                "system notice",
                "visible reply",
                "msg_trigger",
                "[]",
                [],
                reasoning_content="hidden reasoning",
            )

        assistant_inserts = [
            (sql, params)
            for sql, params in statements
            if len(params) >= 3 and params[2] == "assistant"
        ]
        self.assertEqual(len(assistant_inserts), 1)
        sql, params = assistant_inserts[0]
        self.assertIn("reasoning_content", sql)
        self.assertEqual(params[-1], "hidden reasoning")

        events = [call.args[0] for call in broadcast.await_args_list]
        assistant_events = [
            event
            for event in events
            if event["type"] == "msg_created" and event["data"]["id"] == "msg_trigger"
        ]
        self.assertEqual(len(assistant_events), 1)
        self.assertEqual(assistant_events[0]["data"]["reasoning_content"], "hidden reasoning")

    async def test_trigger_debug_broadcast_uses_msg_id_and_usage_meta(self):
        broadcast = AsyncMock()
        usage_meta = {"prompt_tokens": 10, "reasoning_content": "hidden"}
        prompt_messages = [{"role": "user", "content": "trigger prompt"}]

        with patch.object(schedule_module.manager, "broadcast", new=broadcast):
            await schedule_module._broadcast_trigger_debug(
                msg_id="msg_trigger",
                model_key="unit-model",
                usage_meta=usage_meta,
                prompt_messages=prompt_messages,
                recalled_memories=[{"content": "memory"}],
            )

        event = broadcast.await_args.args[0]
        self.assertEqual(event["type"], "debug")
        self.assertEqual(event["data"]["msg_id"], "msg_trigger")
        self.assertEqual(event["data"]["model"], "unit-model")
        self.assertIs(event["data"]["usage"], usage_meta)
        self.assertEqual(event["data"]["prompt_messages"], prompt_messages)
        self.assertEqual(event["data"]["prompt_count"], 1)

    def test_background_meta_is_fresh_per_generation(self):
        first = schedule_module._new_background_meta()
        second = schedule_module._new_background_meta()

        first["reasoning_content"] = "old reasoning"

        self.assertEqual(first["antigravity_print_timeout"], "90s")
        self.assertEqual(second["antigravity_print_timeout"], "90s")
        self.assertNotIn("reasoning_content", second)

    async def test_location_chatroom_wakeup_persists_stream_reasoning(self):
        statements = []
        captured_meta = []

        def fake_get_db():
            return RecordingDbContext(statements, ConversationDb)

        async def fake_stream_ai(messages, model_key, meta=None, temperature=None):
            captured_meta.append(meta)
            if meta is not None:
                meta["reasoning_content"] = "hidden location reasoning"
            yield "visible location reply"

        class FakeScheduleManager:
            def __init__(self):
                self._save_to_chatroom = AsyncMock()

            def _resolve_target(self, payload):
                return {"type": "chatroom", "room_id": "room_test"}

        fake_schedule_mgr = FakeScheduleManager()

        with ExitStack() as stack:
            stack.enter_context(patch("location.get_db", new=fake_get_db))
            stack.enter_context(patch("location.load_worldbook", return_value={
                "ai_name": "Airi",
                "user_name": "User",
                "ai_persona": "kind companion",
                "user_persona": "likes clear answers",
            }))
            stack.enter_context(patch("location.format_location_for_prompt", return_value="当前位置：家附近"))
            stack.enter_context(patch.object(location_module.manager, "any_tts_enabled", return_value=False))
            stack.enter_context(patch.object(location_module.manager, "broadcast", new=AsyncMock()))
            stack.enter_context(patch("camera.append_monitor_log"))
            stack.enter_context(patch("memory.recall_memories", new=AsyncMock(return_value=([], []))))
            stack.enter_context(patch("context_builder.fetch_merged_timeline", new=AsyncMock(return_value=[])))
            stack.enter_context(patch("context_builder.render_merged_timeline", return_value=[]))
            stack.enter_context(patch("ai_providers.stream_ai", new=fake_stream_ai))
            stack.enter_context(patch("schedule.schedule_mgr", new=fake_schedule_mgr))

            await location_module._call_core_location(
                "到家",
                {},
                "位置变化",
                cached_logs=[],
                last_user_ts=0,
            )

        self.assertEqual(len(captured_meta), 1)
        self.assertIsNotNone(captured_meta[0])
        fake_schedule_mgr._save_to_chatroom.assert_awaited_once()
        args = fake_schedule_mgr._save_to_chatroom.await_args.args
        self.assertEqual(args[0], "room_test")
        self.assertEqual(args[-1], "hidden location reasoning")

    async def test_location_wakeup_discards_unvalidated_partial_when_stream_breaks(self):
        statements = []

        def fake_get_db():
            return RecordingDbContext(statements, ConversationDb)

        async def interrupted_stream(messages, model_key, meta=None, temperature=None):
            yield "前半段正文。"
            raise RuntimeError("peer closed connection")

        class FakeScheduleManager:
            def __init__(self):
                self._save_to_chatroom = AsyncMock()

            def _resolve_target(self, payload):
                return {"type": "chatroom", "room_id": "room_test"}

        fake_schedule_mgr = FakeScheduleManager()

        with ExitStack() as stack:
            stack.enter_context(patch("location.get_db", new=fake_get_db))
            stack.enter_context(patch("location.load_worldbook", return_value={
                "ai_name": "Airi",
                "user_name": "User",
                "ai_persona": "kind companion",
                "user_persona": "likes clear answers",
            }))
            stack.enter_context(patch("location.format_location_for_prompt", return_value="当前位置：家附近"))
            stack.enter_context(patch.object(location_module.manager, "any_tts_enabled", return_value=False))
            stack.enter_context(patch.object(location_module.manager, "broadcast", new=AsyncMock()))
            stack.enter_context(patch("camera.append_monitor_log"))
            stack.enter_context(patch("memory.recall_memories", new=AsyncMock(return_value=([], []))))
            stack.enter_context(patch("context_builder.fetch_merged_timeline", new=AsyncMock(return_value=[])))
            stack.enter_context(patch("context_builder.render_merged_timeline", return_value=[]))
            stack.enter_context(patch("ai_providers.stream_ai", new=interrupted_stream))
            stack.enter_context(patch("schedule.schedule_mgr", new=fake_schedule_mgr))

            await location_module._call_core_location(
                "到家",
                {},
                "位置变化",
                cached_logs=[],
                last_user_ts=0,
            )

        fake_schedule_mgr._save_to_chatroom.assert_awaited_once()
        reply_text = fake_schedule_mgr._save_to_chatroom.await_args.args[3]
        self.assertEqual(reply_text, "[回复连接中断，已自动停止生成。]")
        self.assertNotIn("peer closed", reply_text)


if __name__ == "__main__":
    unittest.main()
