import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import chatroom
import database
from routes import chat as chat_routes
from routes import chatroom as chatroom_routes


class RandomModelPoolCoreTests(unittest.TestCase):
    def setUp(self):
        chatroom._LAST_RANDOM_MODEL_BY_ACTOR.clear()

    def test_disabled_or_empty_pool_returns_none(self):
        models = {
            "Model-A": {"provider": "gemini", "model": "g-a"},
            "Model-B": {"provider": "codex_cli", "model": "c-b"},
        }
        self.assertIsNone(
            chatroom.pick_random_model_from_pool(
                "aion",
                {"aion_random_model_enabled": False, "aion_random_models": ["Model-A"]},
                available_models=models,
            )
        )
        self.assertIsNone(
            chatroom.pick_random_model_from_pool(
                "aion",
                {"aion_random_model_enabled": True, "aion_random_models": []},
                available_models=models,
            )
        )

    def test_filters_unavailable_and_deprecated_models_and_picks_from_valid(self):
        models = {
            "Model-A": {"provider": "gemini", "model": "g-a"},
            "Model-Old": {"provider": "gemini_cli", "model": "legacy"},
            "Model-B": {"provider": "custom_openai", "model": "c-b"},
        }
        cfg = {
            "aion_random_model_enabled": True,
            "aion_random_models": ["Missing", "Model-Old", "Model-A", "Model-A", "Model-B"],
        }
        with patch("chatroom.random.choice", side_effect=lambda seq: seq[-1]) as choice_mock:
            picked = chatroom.pick_random_model_from_pool("aion", cfg, available_models=models)

        self.assertEqual(picked, "Model-B")
        choice_mock.assert_called_once_with(["Model-A", "Model-B"])

    def test_connor_pool_allows_codex_alias(self):
        models = {"Model-A": {"provider": "gemini", "model": "g-a"}}
        cfg = {
            "connor_random_model_enabled": True,
            "connor_random_models": ["Codex"],
        }
        picked = chatroom.pick_random_model_from_pool("connor", cfg, available_models=models)
        self.assertEqual(picked, "Codex")

    def test_avoids_consecutive_repeat_when_pool_has_multiple_models(self):
        models = {
            "Model-A": {"provider": "gemini", "model": "g-a"},
            "Model-B": {"provider": "gemini", "model": "g-b"},
            "Model-C": {"provider": "gemini", "model": "g-c"},
        }
        cfg = {
            "aion_random_model_enabled": True,
            "aion_random_models": ["Model-A", "Model-B", "Model-C"],
            "connor_random_model_enabled": True,
            "connor_random_models": ["Model-A", "Model-B"],
        }
        aion_picks = [
            chatroom.pick_random_model_from_pool("aion", cfg, available_models=models)
            for _ in range(12)
        ]
        connor_picks = [
            chatroom.pick_random_model_from_pool("connor", cfg, available_models=models)
            for _ in range(6)
        ]
        for prev, curr in zip(aion_picks, aion_picks[1:]):
            self.assertNotEqual(prev, curr)
        for prev, curr in zip(connor_picks, connor_picks[1:]):
            self.assertNotEqual(prev, curr)


class RandomModelPoolRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chatroom._LAST_RANDOM_MODEL_BY_ACTOR.clear()
    async def test_group_send_uses_both_random_pools_over_fixed_payload(self):
        captured = {}
        body = chatroom_routes.MsgSend(
            content="hello",
            model="fixed-aion",
            connor_model="fixed-connor",
        )

        async def fake_group_replies(room_id, room, msgs, model_key, connor_model_key, queue, context_limit, **kwargs):
            captured["model_key"] = model_key
            captured["connor_model_key"] = connor_model_key
            captured["aion_from_pool"] = kwargs.get("aion_from_pool")
            captured["connor_from_pool"] = kwargs.get("connor_from_pool")

        models = {
            "Pool-A1": {"provider": "gemini", "model": "ga1"},
            "Pool-C1": {"provider": "codex_cli", "model": "gc1"},
        }
        cfg = {
            "aion_model": "fixed-aion",
            "connor_model": "fixed-connor",
            "aion_random_model_enabled": True,
            "aion_random_models": ["Pool-A1"],
            "connor_random_model_enabled": True,
            "connor_random_models": ["Pool-C1"],
            "reply_order": "random",
        }
        with (
            patch.object(chatroom_routes, "MODELS", models),
            patch.object(chatroom_routes, "load_chatroom_config", return_value=cfg),
            patch.object(chatroom_routes, "_save_msg", new=AsyncMock(return_value={"id": "u1", "duplicate": False})),
            patch.object(chatroom_routes, "_load_room_and_messages", new=AsyncMock(return_value=({"id": "r1", "type": "group", "context_minutes": 30}, []))),
            patch.object(chatroom_routes, "record_chatroom_active", new=AsyncMock()),
            patch.object(chatroom_routes, "_generate_group_replies", new=fake_group_replies),
            patch.object(chatroom_routes.cam, "reset_patrol_timer"),
        ):
            response = await chatroom_routes.send_message("r1", body)
            async for _chunk in response.body_iterator:
                pass

        self.assertEqual(captured["model_key"], "Pool-A1")
        self.assertEqual(captured["connor_model_key"], "Pool-C1")
        self.assertTrue(captured["aion_from_pool"])
        self.assertTrue(captured["connor_from_pool"])

    async def test_connor_1v1_send_uses_connor_random_pool(self):
        captured = {}
        body = chatroom_routes.MsgSend(content="hi connor", connor_model="fixed-connor")

        async def fake_private_reply(*args, connor_model_key, **kwargs):
            captured["connor_model_key"] = connor_model_key
            captured["random_pool"] = kwargs.get("random_pool")

        models = {"Pool-C1": {"provider": "codex_cli", "model": "gc1"}}
        cfg = {
            "connor_model": "fixed-connor",
            "connor_random_model_enabled": True,
            "connor_random_models": ["Pool-C1"],
        }
        with (
            patch.object(chatroom_routes, "MODELS", models),
            patch.object(chatroom_routes, "load_chatroom_config", return_value=cfg),
            patch.object(chatroom_routes, "_save_msg", new=AsyncMock(return_value={"id": "u1", "duplicate": False})),
            patch.object(chatroom_routes, "_load_room_and_messages", new=AsyncMock(return_value=({"id": "r2", "type": "connor_1v1", "context_minutes": 30}, []))),
            patch.object(chatroom_routes, "record_chatroom_active", new=AsyncMock()),
            patch.object(chatroom_routes, "_generate_connor_reply", new=fake_private_reply),
        ):
            response = await chatroom_routes.send_message("r2", body)
            async for _chunk in response.body_iterator:
                pass

        self.assertEqual(captured["connor_model_key"], "Pool-C1")
        self.assertTrue(captured["random_pool"])

    async def test_ai_chat_picks_models_per_round(self):
        aion_calls = []
        connor_calls = []

        async def fake_reply_aion(*args, **kwargs):
            aion_calls.append((args[4], kwargs.get("random_pool")))
            return None

        async def fake_reply_connor(*args, connor_model_key, **kwargs):
            connor_calls.append((connor_model_key, kwargs.get("random_pool")))
            return None

        models = {
            "A1": {"provider": "gemini", "model": "a1"},
            "A2": {"provider": "gemini", "model": "a2"},
            "C1": {"provider": "codex_cli", "model": "c1"},
            "C2": {"provider": "codex_cli", "model": "c2"},
        }
        cfg = {
            "aion_random_model_enabled": True,
            "aion_random_models": ["A1", "A2"],
            "connor_random_model_enabled": True,
            "connor_random_models": ["C1", "C2"],
            "reply_order": "aion",
        }
        seq = iter(["A1", "C1", "A2", "C2"])
        with (
            patch.object(chatroom_routes, "MODELS", models),
            patch.object(chatroom_routes, "load_chatroom_config", return_value=cfg),
            patch.object(chatroom_routes, "_load_room_and_messages", new=AsyncMock(return_value=({"id": "r1", "type": "group", "context_minutes": 30}, [{"content": "hi"}]))),
            patch.object(chatroom_routes, "_reply_aion", new=fake_reply_aion),
            patch.object(chatroom_routes, "_reply_connor", new=fake_reply_connor),
            patch("chatroom.random.choice", side_effect=lambda _pool: next(seq)),
        ):
            response = await chatroom_routes.trigger_ai_chat(
                "r1",
                chatroom_routes.AiChatTrigger(rounds=2),
            )
            async for _chunk in response.body_iterator:
                pass

        self.assertEqual(aion_calls, [("A1", True), ("A2", True)])
        self.assertEqual(connor_calls, [("C1", True), ("C2", True)])

    async def test_digest_keeps_fixed_connor_model_even_when_pool_enabled(self):
        cfg = {
            "connor_model": "Fixed-Connor",
            "connor_random_model_enabled": True,
            "connor_random_models": ["Pool-C1", "Pool-C2"],
        }
        models = {
            "Fixed-Connor": {"provider": "codex_cli", "model": "fc"},
            "Pool-C1": {"provider": "codex_cli", "model": "c1"},
            "Pool-C2": {"provider": "codex_cli", "model": "c2"},
        }
        with (
            patch.object(chatroom_routes, "MODELS", models),
            patch.object(chatroom_routes, "load_chatroom_config", return_value=cfg),
            patch.object(chatroom_routes, "digest_chatroom", new=AsyncMock(return_value={"ok": True})) as digest_mock,
        ):
            await chatroom_routes.trigger_digest("r1")

        digest_mock.assert_awaited_once_with(model_key="Fixed-Connor")

    def test_private_chat_resolver_respects_allow_random_flag(self):
        models = {
            "Fixed-Aion": {"provider": "gemini", "model": "fa"},
            "Pool-A1": {"provider": "gemini", "model": "pa1"},
        }
        cfg = {
            "aion_random_model_enabled": True,
            "aion_random_models": ["Pool-A1"],
        }
        with (
            patch.object(chat_routes, "MODELS", models),
            patch.object(chat_routes, "resolve_model_key", side_effect=lambda k: k),
            patch("chatroom.load_chatroom_config", return_value=cfg),
        ):
            self.assertEqual(
                chat_routes._resolve_private_chat_model("Fixed-Aion", allow_random=True),
                ("Pool-A1", True),
            )
            self.assertEqual(
                chat_routes._resolve_private_chat_model("Fixed-Aion", allow_random=False),
                ("Fixed-Aion", False),
            )

    def test_config_update_normalizes_random_model_pools(self):
        cfg = chatroom_routes._apply_config_update(
            chatroom._normalize_chatroom_config({}),
            chatroom_routes.ConfigUpdate(
                aion_random_model_enabled=True,
                aion_random_models=[" M1 ", "M1", "", "M2"],
                connor_random_model_enabled=False,
                connor_random_models=["Codex", "Codex "],
            ),
        )
        self.assertTrue(cfg["aion_random_model_enabled"])
        self.assertEqual(cfg["aion_random_models"], ["M1", "M2"])
        self.assertFalse(cfg["connor_random_model_enabled"])
        self.assertEqual(cfg["connor_random_models"], ["Codex"])

    def test_debug_payload_exposes_model_key_and_random_flag(self):
        payload = chatroom_routes._chatroom_debug_payload(
            room_id="r1",
            model_key="Pool-A1",
            msg_id="m1",
            digest_result=None,
            random_pool=True,
        )
        self.assertEqual(payload["model"], "Pool-A1")
        self.assertEqual(payload["model_key"], "Pool-A1")
        self.assertTrue(payload["random_pool"])

    async def test_save_msg_and_private_insert_persist_model_key(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""
                CREATE TABLE chatroom_rooms (id TEXT PRIMARY KEY, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY, room_id TEXT, sender TEXT, content TEXT,
                    attachments TEXT, reasoning_content TEXT, model_key TEXT DEFAULT '', created_at REAL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT,
                    created_at REAL, attachments TEXT, reasoning_content TEXT, model_key TEXT DEFAULT ''
                );
                INSERT INTO chatroom_rooms VALUES ('r1', 'group', 0);
            """)

            @asynccontextmanager
            async def fake_db():
                yield db

            with (
                patch.object(chatroom_routes, "get_db", fake_db),
                patch.object(chatroom_routes, "broadcast_synced", new=AsyncMock()),
                patch.object(chatroom_routes, "connor_1v1_on_message"),
                patch.object(chatroom_routes, "_with_link_previews", new=AsyncMock(side_effect=lambda _t, a: a)),
                patch.object(chatroom_routes, "with_band_vibration_attachment", new=AsyncMock(side_effect=lambda _id, a: a)),
            ):
                saved = await chatroom_routes._save_msg(
                    "r1", "aion", "你好", msg_id="m_cr_1", auto_tts=False, model_key="Pool-A1"
                )
            self.assertEqual(saved["model_key"], "Pool-A1")
            cur = await db.execute("SELECT model_key FROM chatroom_messages WHERE id='m_cr_1'")
            self.assertEqual((await cur.fetchone())[0], "Pool-A1")

            await chat_routes._insert_private_assistant_message(
                db,
                msg_id="m_priv_1",
                conv_id="c1",
                content="hi",
                created_at=1.0,
                attachments_json="",
                reasoning_content="",
                model_key="Pool-A2",
            )
            cur2 = await db.execute("SELECT model_key FROM messages WHERE id='m_priv_1'")
            self.assertEqual((await cur2.fetchone())[0], "Pool-A2")


if __name__ == "__main__":
    unittest.main()
