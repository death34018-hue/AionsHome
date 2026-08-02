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
    async def test_aion_group_context_recalls_memory_even_when_digest_says_no_search(self):
        captured_recall = {}

        async def fake_chatroom_recall(query, room_id="", scope="group", query_keywords=None, **kwargs):
            captured_recall.update(
                {
                    "query": query,
                    "room_id": room_id,
                    "scope": scope,
                    "query_keywords": query_keywords,
                    "kwargs": kwargs,
                }
            )
            return [
                _memory("mem1", "memory alpha"),
                _memory("mem2", "memory beta"),
                _memory("mem3", "memory gamma"),
            ]

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
            patch("chatroom.recall_chatroom_memories", new=fake_chatroom_recall),
            patch("context_builder.build_health_summary", new=_empty_health),
            patch("context_builder.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
            patch("context_builder.recall_memories", new=AsyncMock(return_value=([], []))),
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
        self.assertIn("memory alpha", prompt_text)
        self.assertEqual(len(debug.get("recalled_memories") or []), 3)
        self.assertEqual(captured_recall["kwargs"].get("top_k"), 5)
        self.assertEqual(captured_recall["kwargs"].get("min_results"), 3)

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
