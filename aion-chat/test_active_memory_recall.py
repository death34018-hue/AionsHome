import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chatroom
import context_builder
from routes import chat as chat_routes


def _memory(mem_id: str, content: str, score: float = 0.72) -> dict:
    return {
        "id": mem_id,
        "content": content,
        "type": "event",
        "score": score,
        "vec_sim": score,
        "kw_score": 0.0,
        "importance": 0.5,
        "source_start_ts": None,
        "source_end_ts": None,
        "evidence_summary": "",
    }


async def _empty_health(*args, **kwargs):
    return ""


class ActiveMemoryRecallTests(unittest.IsolatedAsyncioTestCase):
    def test_recall_query_falls_back_to_latest_user_message_when_digest_has_no_clues(self):
        query = context_builder._build_recall_query(
            "",
            [],
            query_text="  last user message  ",
            recent_messages=[],
            status="",
        )

        self.assertEqual(query, "last user message")

    async def test_memory_blocks_recall_summary_without_search_signal(self):
        recalled = _memory("main1", "always surfaced by vector recall")
        digest = {
            "is_search_needed": False,
            "keywords": ["current"],
            "require_detail": False,
            "status": "",
            "topic": "current topic",
        }

        with (
            patch("context_builder.build_health_summary", new=_empty_health),
            patch("context_builder.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
            patch("context_builder.recall_memories", new=AsyncMock(return_value=([recalled], [recalled]))),
        ):
            result = await context_builder.build_memory_blocks(
                "current user message",
                recent_messages=[],
                digest_result=digest,
            )

        self.assertIn("always surfaced by vector recall", result["memory_block"])

    async def test_memory_blocks_formats_surfaced_memories_without_private_helper(self):
        surfaced = [
            _memory("surface1", "ordinary surfaced memory"),
            {**_memory("surface2", "unfinished surfaced memory"), "unresolved": True},
        ]
        digest = {
            "is_search_needed": False,
            "keywords": [],
            "require_detail": False,
            "status": "",
            "topic": "current topic",
        }

        with (
            patch("context_builder.build_health_summary", new=_empty_health),
            patch(
                "context_builder.build_surfacing_memories",
                new=AsyncMock(return_value=(surfaced, {"surface1", "surface2"})),
            ),
            patch("context_builder.recall_memories", new=AsyncMock(return_value=([], []))),
        ):
            result = await context_builder.build_memory_blocks(
                "current user message",
                recent_messages=[],
                digest_result=digest,
            )

        self.assertIn("- 记忆：ordinary surfaced memory", result["time_block"])
        self.assertIn(
            "📌 记忆：unfinished surfaced memory（还没做/还没去）",
            result["time_block"],
        )

    async def test_search_signal_includes_recalled_memory_source_text(self):
        recalled = _memory("main1", "memory with source")
        recalled["source_start_ts"] = 1000.0
        digest = {
            "is_search_needed": True,
            "keywords": ["past"],
            "require_detail": False,
            "status": "",
            "topic": "past topic",
        }
        source_lookup = AsyncMock(return_value="historical source text")

        with (
            patch("context_builder.build_health_summary", new=_empty_health),
            patch("context_builder.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
            patch("context_builder.recall_memories", new=AsyncMock(return_value=([recalled], [recalled]))),
            patch("context_builder.fetch_source_details", new=source_lookup),
        ):
            result = await context_builder.build_memory_blocks(
                "what happened before",
                recent_messages=[],
                digest_result=digest,
            )

        source_lookup.assert_awaited_once()
        self.assertIn("historical source text", result["memory_block"])

    async def test_aion_group_context_recalls_only_main_memory(self):
        chatroom_recall_called = False

        async def forbidden_chatroom_recall(*args, **kwargs):
            nonlocal chatroom_recall_called
            chatroom_recall_called = True
            return [_memory("companion1", "companion memory")]

        digest = {
            "is_search_needed": False,
            "keywords": [],
            "require_detail": False,
            "status": "",
            "topic": "",
        }
        merged = [
            {
                "sender": "user",
                "content": "hello current turn",
                "created_at": 1000.0,
                "attachments": "[]",
                "source": "group",
            }
        ]

        patches = [
            patch("chatroom.fetch_merged_timeline", new=AsyncMock(return_value=merged)),
            patch("chatroom.load_worldbook", return_value={}),
            patch("chatroom.get_chatroom_names", return_value=("User", "MainAI", "Companion")),
            patch("chatroom.build_ability_block", new=AsyncMock(return_value="")),
            patch("chatroom.recall_chatroom_memories", new=forbidden_chatroom_recall),
            patch("context_builder.build_health_summary", new=_empty_health),
            patch("context_builder.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
            patch(
                "context_builder.recall_memories",
                new=AsyncMock(
                    return_value=(
                        [_memory("main1", "main memory")],
                        [_memory("main1", "main memory")],
                    )
                ),
            ),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            history, debug = await chatroom.build_aion_group_context(
                "room1",
                [],
                context_limit=10,
                query_text="hello current turn",
                digest_result=digest,
            )

        prompt_text = "\n".join(str(m.get("content", "")) for m in history)
        self.assertIn("main memory", prompt_text)
        self.assertNotIn("companion memory", prompt_text)
        self.assertFalse(chatroom_recall_called)
        self.assertEqual(len(debug.get("recalled_memories") or []), 1)

    async def test_companion_recall_filters_legacy_store_by_scope(self):
        executed = {}

        class Cursor:
            async def fetchall(self):
                return []

        class Db:
            row_factory = None

            async def execute(self, sql, params=()):
                executed["sql"] = sql
                executed["params"] = params
                return Cursor()

        class DbContext:
            async def __aenter__(self):
                return Db()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch("chatroom.get_embedding", new=AsyncMock(return_value=None)),
            patch("chatroom.get_db", return_value=DbContext()),
        ):
            await chatroom.recall_chatroom_memories(
                "query",
                room_id="room1",
                scope="connor",
                min_results=3,
            )

        self.assertIn("scope = ?", executed["sql"])
        self.assertEqual(executed["params"][0], "connor")

    async def test_companion_background_surfacing_filters_every_query_by_scope(self):
        executed = []

        class Cursor:
            async def fetchall(self):
                return []

        class Db:
            row_factory = None

            async def execute(self, sql, params=()):
                executed.append((sql, params))
                return Cursor()

        class DbContext:
            async def __aenter__(self):
                return Db()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch("chatroom.get_embedding", new=AsyncMock(return_value=[1.0, 0.0])),
            patch("chatroom.get_db", return_value=DbContext()),
        ):
            await chatroom.build_surfacing_chatroom_memories(
                topic="scope isolation probe",
                keywords=[],
                max_total=8,
            )

        memory_queries = [
            (sql, params)
            for sql, params in executed
            if "FROM chatroom_memories" in sql
        ]
        self.assertEqual(len(memory_queries), 3)
        for sql, params in memory_queries:
            self.assertIn("scope = ?", sql)
            self.assertIn("connor", params)

    async def test_private_chat_regenerate_injects_recalled_memory_without_search_signal(self):
        captured = {}

        async def fake_stream_ai(messages, *args, **kwargs):
            captured["messages"] = messages
            yield "regenerated"

        rendered_history = [
            {"role": "user", "content": "older user prompt", "attachments": []},
            {"role": "assistant", "content": "older reply", "attachments": []},
            {"role": "user", "content": "latest user prompt", "attachments": []},
        ]

        patches = [
            patch("routes.chat.get_db", new=_fake_get_db),
            patch("routes.chat.resolve_model_key", return_value="unit-model"),
            patch("routes.chat.fetch_merged_timeline", new=AsyncMock(return_value=[])),
            patch("routes.chat.render_merged_timeline", return_value=list(rendered_history)),
            patch("routes.chat.load_worldbook", return_value={}),
            patch("routes.chat._insert_private_ability_block", new=AsyncMock(return_value=0)),
            patch(
                "routes.chat.instant_digest",
                new=AsyncMock(
                    return_value={
                        "keywords": [],
                        "topic": "unit topic",
                        "is_search_needed": False,
                        "status": "",
                        "require_detail": False,
                    }
                ),
            ),
            patch("routes.chat.build_health_summary", new=AsyncMock(return_value="")),
            patch("routes.chat.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
            patch("routes.chat.recall_memories", new=AsyncMock(return_value=([_memory("mem1", "private memory")], [_memory("mem1", "private memory")]))),
            patch("routes.chat.stream_ai", new=fake_stream_ai),
            patch("routes.chat.process_schedule_commands", new=AsyncMock(side_effect=lambda text, *a, **k: text)),
            patch("routes.chat._process_home_commands", new=AsyncMock(side_effect=lambda text: text)),
            patch("routes.chat.handle_luckin_commands", new=AsyncMock(side_effect=lambda text: (text, []))),
            patch("routes.chat._process_wish_commands", new=AsyncMock(side_effect=lambda text, **k: text)),
            patch("routes.chat._extract_reply_image_attachments", side_effect=lambda text: (text, [])),
            patch("routes.chat.luckin_payment_attachments", return_value=[]),
            patch("routes.chat.export_conversation", new=AsyncMock()),
            patch.object(chat_routes.manager, "broadcast", new=AsyncMock()),
            patch.object(chat_routes.manager, "set_tts_fallback", new=Mock()),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            response = await chat_routes.regenerate_message("conv_test", context_limit=10)
            async for _ in response.body_iterator:
                pass

        prompt_text = "\n".join(str(m.get("content", "")) for m in captured["messages"])
        self.assertIn("private memory", prompt_text)


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return []


class _FakeDb:
    def __init__(self):
        self.row_factory = None

    async def execute(self, sql, params=()):
        if "SELECT model FROM conversations" in sql:
            return _FakeCursor({"model": "unit-model"})
        return _FakeCursor()

    async def commit(self):
        return None


class _FakeDbContext:
    async def __aenter__(self):
        return _FakeDb()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_get_db():
    return _FakeDbContext()


if __name__ == "__main__":
    unittest.main()
