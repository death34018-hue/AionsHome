import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


def _create_fixture(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE theater_conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            persona_id TEXT,
            model TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE theater_messages (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            attachments TEXT DEFAULT '[]',
            reasoning_content TEXT DEFAULT ''
        );
        INSERT INTO theater_conversations VALUES
            ('tc_regen', 'test', 'old-persona', 'old-model', 1, 1);
        INSERT INTO theater_messages
            (id, conv_id, role, content, created_at, attachments)
            VALUES ('tm_user', 'tc_regen', 'user', 'write a scene', 2, '[]');
        INSERT INTO theater_messages
            (id, conv_id, role, content, created_at, attachments)
            VALUES ('tm_old_ai', 'tc_regen', 'assistant', 'discard this reply', 3, '[]');
        """
    )
    conn.commit()
    conn.close()


class TheaterRegenerateTests(unittest.IsolatedAsyncioTestCase):
    async def test_regenerate_deletes_target_and_uses_requested_configuration(self):
        import database
        from routes import theater as route

        self.assertTrue(
            hasattr(route, "TheaterRegenerateRequest"),
            "regeneration needs an explicit request containing the target and current configuration",
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "tts"
            cache_dir.mkdir()
            _create_fixture(db_path)
            (cache_dir / "tm_old_ai.mp3").write_bytes(b"old audio")
            (cache_dir / "tm_old_ai_s0.mp3").write_bytes(b"old segment")

            captured = {}

            async def fake_stream_ai(history, model, usage_meta, **kwargs):
                captured["history"] = history
                captured["model"] = model
                yield "new reply"

            old_db_path = database.DB_PATH
            old_cache_dir = route.THEATER_TTS_CACHE_DIR
            database.DB_PATH = db_path
            route.THEATER_TTS_CACHE_DIR = cache_dir
            request = route.TheaterRegenerateRequest(
                message_id="tm_old_ai",
                model="new-model",
                persona_id="new-persona",
            )
            try:
                with (
                    patch.object(route, "stream_ai", new=fake_stream_ai),
                    patch.object(
                        route,
                        "_load_personas",
                        return_value=[{"id": "new-persona", "persona": "fresh persona text"}],
                    ),
                    patch.object(route.manager, "broadcast", new=AsyncMock()) as broadcast,
                ):
                    response = await route.regenerate_message(
                        "tc_regen",
                        body=request,
                        context_limit=20,
                    )

                    conn = sqlite3.connect(db_path)
                    ids_after_acceptance = [
                        row[0] for row in conn.execute(
                            "SELECT id FROM theater_messages ORDER BY created_at"
                        )
                    ]
                    conn.close()

                    body = ""
                    async for chunk in response.body_iterator:
                        body += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

                self.assertEqual(ids_after_acceptance, ["tm_user"])
                self.assertFalse((cache_dir / "tm_old_ai.mp3").exists())
                self.assertFalse((cache_dir / "tm_old_ai_s0.mp3").exists())
                self.assertEqual(captured["model"], "new-model")
                self.assertNotIn("discard this reply", json.dumps(captured["history"]))
                self.assertEqual(
                    captured["history"][:2],
                    [
                        {"role": "user", "content": "[角色设定]\nfresh persona text"},
                        {"role": "assistant", "content": "收到，我会按照设定扮演角色。"},
                    ],
                )
                broadcast.assert_any_await(
                    {
                        "type": "theater_msg_deleted",
                        "data": {"id": "tm_old_ai", "conv_id": "tc_regen"},
                    }
                )
                self.assertIn("new reply", body)
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

    def test_frontend_sends_selected_message_model_and_persona(self):
        html = (Path(__file__).parent / "static" / "theater.html").read_text(encoding="utf-8")

        self.assertIn("regenerateMsg('${m.id}')", html)
        self.assertIn("message_id: aiMsgId", html)
        self.assertIn("model: $('modelSelect').value", html)
        self.assertIn("persona_id: currentPersonaId", html)


if __name__ == "__main__":
    unittest.main()
