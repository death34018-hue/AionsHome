import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


def _create_theater_db(db_path: Path, persona_id: str = "p1"):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        f"""
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
            ('tc_1', '测试剧场', '{persona_id}', 'test-model', 1, 1);
        INSERT INTO theater_messages
            (id, conv_id, role, content, created_at, attachments)
            VALUES ('tm_u1', 'tc_1', 'user', '第一句', 2, '[]');
        INSERT INTO theater_messages
            (id, conv_id, role, content, created_at, attachments)
            VALUES ('tm_a1', 'tc_1', 'assistant', '旧回复', 3, '[]');
        """
    )
    conn.commit()
    conn.close()


class TheaterPromptModulesTests(unittest.IsolatedAsyncioTestCase):
    def test_compose_persona_text_with_global_modules(self):
        from routes.theater import compose_persona_text

        self.assertEqual(compose_persona_text(None), "")
        self.assertEqual(
            compose_persona_text({"id": "p", "name": "角色", "persona": "主设定"}),
            "主设定",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "主设定"},
                [
                    {"id": "writing_prompt_1", "title": "写作提示1", "enabled": False, "content": "未勾选内容1"},
                    {"id": "writing_prompt_2", "title": "写作提示2", "enabled": False, "content": "未勾选内容2"},
                ],
            ),
            "主设定",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "主设定"},
                [
                    {"id": "writing_prompt_1", "enabled": True, "content": "  写作提示一  "},
                    {"id": "writing_prompt_2", "enabled": False, "content": "禁止发送二"},
                ],
            ),
            "主设定\n\n写作提示一",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "主设定"},
                [
                    {"id": "writing_prompt_1", "enabled": False, "content": "禁止发送一"},
                    {"id": "writing_prompt_2", "enabled": True, "content": "禁令二"},
                ],
            ),
            "主设定\n\n禁令二",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "主设定"},
                [
                    {"id": "writing_prompt_1", "enabled": True, "content": "提示一"},
                    {"id": "writing_prompt_2", "enabled": True, "content": "禁令二"},
                ],
            ),
            "主设定\n\n提示一\n\n禁令二",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "主设定"},
                [
                    {"id": "writing_prompt_1", "enabled": True, "content": "   \n\t  "},
                    {"id": "writing_prompt_2", "enabled": False, "content": "未勾选"},
                ],
            ),
            "主设定",
        )
        self.assertEqual(
            compose_persona_text(
                None,
                [{"id": "writing_prompt_1", "enabled": True, "content": "仅通用写作提示"}],
            ),
            "仅通用写作提示",
        )
        self.assertEqual(
            compose_persona_text(
                {"persona": "   "},
                [{"id": "writing_prompt_1", "enabled": False, "content": "未勾选"}],
            ),
            "",
        )

    async def test_shared_prompt_modules_crud_and_readonly_legacy_fallback(self):
        from routes import theater as route

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            personas_path = root / "theater_personas.json"
            modules_path = root / "theater_prompt_modules.json"
            personas_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "p_a",
                            "name": "角色A",
                            "persona": "人设A",
                            "prompt_modules": [
                                {"id": "writing_prompt_1", "title": "写作提示1", "enabled": False, "content": "未勾选旧草稿"},
                                {"id": "writing_prompt_2", "title": "写作提示2", "enabled": False, "content": "仅非空未勾选二"},
                            ],
                        },
                        {
                            "id": "p_b",
                            "name": "角色B",
                            "persona": "人设B",
                            "prompt_modules": [
                                {"id": "writing_prompt_1", "title": "写作提示1", "enabled": True, "content": "已勾选优先一"},
                                {"id": "writing_prompt_2", "title": "写作提示2", "enabled": False, "content": ""},
                            ],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(route, "PERSONAS_PATH", personas_path),
                patch.object(route, "PROMPT_MODULES_PATH", modules_path),
            ):
                migrated = await route.list_prompt_modules()
                self.assertFalse(modules_path.exists())
                self.assertEqual(
                    migrated,
                    [
                        {"id": "writing_prompt_1", "title": "写作提示1", "enabled": True, "content": "已勾选优先一"},
                        {"id": "writing_prompt_2", "title": "写作提示2", "enabled": False, "content": "仅非空未勾选二"},
                    ],
                )

                saved = await route.update_prompt_modules(
                    route.PromptModulesUpdate(
                        prompt_modules=[
                            route.PersonaPromptModule(id="writing_prompt_1", enabled=False, content="新版提示一"),
                            route.PersonaPromptModule(id="writing_prompt_2", enabled=True, content="新版禁令二"),
                        ]
                    )
                )
                self.assertTrue(modules_path.exists())
                self.assertEqual(
                    saved,
                    [
                        {"id": "writing_prompt_1", "title": "写作提示1", "enabled": False, "content": "新版提示一"},
                        {"id": "writing_prompt_2", "title": "写作提示2", "enabled": True, "content": "新版禁令二"},
                    ],
                )
                self.assertEqual(await route.list_prompt_modules(), saved)

                created = await route.create_persona(route.PersonaCreate(name="角色C", persona="人设C"))
                self.assertNotIn("prompt_modules", created)
                updated = await route.update_persona(created["id"], route.PersonaUpdate(name="角色C改"))
                self.assertEqual(updated["name"], "角色C改")
                self.assertNotIn("prompt_modules", updated)

    async def test_shared_prompt_modules_follow_persona_switches_in_dialogue_and_regenerate(self):
        import database
        import theater_studio
        from routes import theater as route

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "tts"
            cache_dir.mkdir()
            personas_path = root / "theater_personas.json"
            modules_path = root / "theater_prompt_modules.json"
            _create_theater_db(db_path, persona_id="p1")
            personas_path.write_text(
                json.dumps(
                    [
                        {"id": "p1", "name": "角色甲", "persona": "人设甲正文"},
                        {"id": "p2", "name": "角色乙", "persona": "人设乙正文"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            modules_path.write_text(
                json.dumps(
                    [
                        {"id": "writing_prompt_1", "title": "写作提示1", "enabled": True, "content": "全局启用提示一"},
                        {"id": "writing_prompt_2", "title": "写作提示2", "enabled": False, "content": "全局未启用提示二"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            captured_histories = []

            async def fake_stream_ai(history, model, usage_meta, **kwargs):
                captured_histories.append(history)
                yield "新回复"

            old_db_path = database.DB_PATH
            old_cache_dir = route.THEATER_TTS_CACHE_DIR
            database.DB_PATH = db_path
            route.THEATER_TTS_CACHE_DIR = cache_dir
            try:
                with (
                    patch.object(route, "PERSONAS_PATH", personas_path),
                    patch.object(route, "PROMPT_MODULES_PATH", modules_path),
                    patch.object(route, "stream_ai", new=fake_stream_ai),
                    patch.object(route.manager, "broadcast", new=AsyncMock()),
                ):
                    send_resp = await route.send_message(
                        "tc_1",
                        route.MsgCreate(content="第二句", context_limit=20),
                    )
                    async for _ in send_resp.body_iterator:
                        pass

                    regen_resp = await route.regenerate_message(
                        "tc_1",
                        body=route.TheaterRegenerateRequest(
                            message_id="tm_a1",
                            model="test-model",
                            persona_id="p2",
                        ),
                        context_limit=20,
                    )
                    async for _ in regen_resp.body_iterator:
                        pass
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir
                theater_studio._live.clear()

            self.assertEqual(len(captured_histories), 2)
            self.assertEqual(
                captured_histories[0][:2],
                [
                    {"role": "user", "content": "[角色设定]\n人设甲正文\n\n全局启用提示一"},
                    {"role": "assistant", "content": "收到，我会按照设定扮演角色。"},
                ],
            )
            self.assertEqual(
                captured_histories[1][:2],
                [
                    {"role": "user", "content": "[角色设定]\n人设乙正文\n\n全局启用提示一"},
                    {"role": "assistant", "content": "收到，我会按照设定扮演角色。"},
                ],
            )
            for history in captured_histories:
                self.assertNotIn("全局未启用提示二", json.dumps(history, ensure_ascii=False))

    async def test_studio_and_planning_use_shared_modules_while_writer_ready_requires_persona(self):
        import theater_planning as planning
        import theater_studio as studio
        from routes import theater as route

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "studio.db"
            personas_path = root / "theater_personas.json"
            modules_path = root / "theater_prompt_modules.json"
            personas_path.write_text(
                json.dumps(
                    [
                        {"id": "p_a", "name": "小说角色A", "persona": "主角色设定A"},
                        {"id": "p_b", "name": "小说角色B", "persona": "主角色设定B"},
                        {"id": "p_empty", "name": "空角色", "persona": "   "},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            modules_path.write_text(
                json.dumps(
                    [
                        {"id": "writing_prompt_1", "title": "写作提示1", "enabled": False, "content": "未启用提示一"},
                        {"id": "writing_prompt_2", "title": "写作提示2", "enabled": True, "content": "已启用通用禁令二"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            async with aiosqlite.connect(db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE theater_conversations(
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        model TEXT,
                        persona_id TEXT,
                        created_at REAL,
                        updated_at REAL
                    );
                    INSERT INTO theater_conversations(id,title,model,persona_id) VALUES
                        ('book_a', '小说A', 'test-model', 'p_a'),
                        ('book_b', '小说B', 'test-model', 'p_b'),
                        ('book_empty', '空角色小说', 'test-model', 'p_empty');
                    """
                )
                await db.commit()

            with (
                patch.object(studio, "get_db", lambda: aiosqlite.connect(db_path)),
                patch.object(route, "PERSONAS_PATH", personas_path),
                patch.object(route, "PROMPT_MODULES_PATH", modules_path),
            ):
                for book_id, chap_id in (("book_a", "chap_a"), ("book_b", "chap_b")):
                    await studio.save(
                        dict(
                            id=book_id,
                            kind="book",
                            conv_id=book_id,
                            **studio.BookPatch(mode="novel", illustrations=False).model_dump(),
                        )
                    )
                    await studio.change(book_id, phase="writing", consensus="雨夜初遇", outline="相遇")
                    await studio.save(
                        dict(
                            id=chap_id,
                            kind="chapter",
                            conv_id=book_id,
                            number=1,
                            title="第一章",
                            plan="开篇计划",
                            content="",
                            status="planned",
                            revision=1,
                            summary="",
                            images=[],
                            versions=[],
                            audio=None,
                        )
                    )

                captured_prompts = []

                async def fake_stream_ai(messages, *args, **kwargs):
                    captured_prompts.append(messages[0]["content"])
                    yield "章节正文。"

                with (
                    patch.object(studio, "stream_ai", fake_stream_ai),
                    patch.object(studio, "summarize", AsyncMock(return_value="摘要")),
                    patch.object(studio, "illustrate", AsyncMock()),
                ):
                    await studio.generate_text("book_a", "讨论A")
                    await studio.write_chapter("chap_a", "写A")
                    await studio.generate_text("book_b", "讨论B")

                self.assertEqual(len(captured_prompts), 3)
                self.assertIn("主角色设定A\n\n已启用通用禁令二", captured_prompts[0])
                self.assertIn("主角色设定A\n\n已启用通用禁令二", captured_prompts[1])
                self.assertIn("主角色设定B\n\n已启用通用禁令二", captured_prompts[2])
                for prompt in captured_prompts:
                    self.assertNotIn("未启用提示一", prompt)

                chapter_a = await studio.load("chap_a")
                self.assertEqual(
                    chapter_a["context_characters"]["角色人设"],
                    len("主角色设定A\n\n已启用通用禁令二"),
                )

                book_empty = await studio.get_book("book_empty")
                self.assertFalse(book_empty["book"]["writer_ready"])
                with self.assertRaises(studio.HTTPException) as exc:
                    await planning.persona_for("book_empty", required=True)
                self.assertEqual(exc.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
