import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

import book
import database
from routes import chat as chat_routes
from routes import chatroom as chatroom_routes


def _create_books_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE books (
            book_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT DEFAULT '未知作者',
            cover_path TEXT,
            total_chapters INTEGER DEFAULT 0,
            current_chapter INTEGER DEFAULT 0,
            current_paragraph INTEGER DEFAULT 0,
            import_time REAL NOT NULL
        );
        CREATE TABLE book_chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            chapter_index INTEGER NOT NULL,
            title TEXT,
            html_content TEXT,
            text_content TEXT,
            paragraphs TEXT,
            char_count INTEGER DEFAULT 0,
            segment_count INTEGER DEFAULT 0,
            segments_meta TEXT DEFAULT '[]',
            UNIQUE(book_id, chapter_index)
        );
        CREATE TABLE book_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            chapter_index INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            annotations TEXT DEFAULT '[]',
            summary TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL,
            annotator TEXT DEFAULT 'aion'
        );
        """
    )
    conn.commit()
    conn.close()


class ReadingSelectionContextTests(unittest.IsolatedAsyncioTestCase):
    def test_format_reading_selection_context_budgets_and_clipping(self):
        long_prev = "前" * 1500
        long_target = "中" * 1500
        long_next = "后" * 900
        ctx = book.format_reading_selection_context(
            book_title="星夜来信",
            author="林声",
            chapter_index=0,
            chapter_title="第一章 初雪",
            paragraphs=[long_prev, long_target, long_next],
            start_p=1,
            end_p=1,
            actor_summary="这段情绪很克制。",
            actor_annotations=["共鸣：这句很有画面感"],
        )
        self.assertIn("当前共读：《星夜来信》｜林声｜第一章 初雪", ctx)
        self.assertIn("【上文】\n……" + "前" * 1200, ctx)
        self.assertNotIn("前" * 1201, ctx)
        self.assertIn("【选中所在段落】\n" + "中" * 1200 + "……", ctx)
        self.assertNotIn("中" * 1201, ctx)
        self.assertIn("【下文】\n" + "后" * 600 + "……", ctx)
        self.assertNotIn("后" * 601, ctx)
        self.assertIn("【你之前读本段的读后感】\n这段情绪很克制。", ctx)
        self.assertIn("【你在此处的批注】\n共鸣：这句很有画面感", ctx)

    async def test_load_reading_selection_context_fallback_validation_and_actor_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "books.db"
            _create_books_db(db_path)
            paragraphs = [
                "第一段：前情铺垫A。" + "甲" * 700,
                "第二段：前情铺垫B。" + "乙" * 600,
                "第三段：窗台上落了很轻的一层雪。",
                "第四段：他把信纸折好放进口袋。",
                "第五段：后续尾声C。" + "丙" * 350,
                "第六段：后续尾声D。" + "丁" * 400,
            ]
            segments_meta = [
                {"start_p": 0, "end_p": 3, "status": "done"},
                {"start_p": 4, "end_p": 5, "status": "done"},
            ]
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("b1", "冬夜手记", "未知作者", None, 1, 0, 0, 1.0),
            )
            conn.execute(
                "INSERT INTO book_chapters (id, book_id, chapter_index, title, html_content, text_content, paragraphs, char_count, segment_count, segments_meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (101, "b1", 0, "雪落窗前", "", "", json.dumps(paragraphs, ensure_ascii=False), 100, 2, json.dumps(segments_meta)),
            )
            conn.execute(
                "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                ("b1", 0, 0, json.dumps([{"p": 2, "type": "共鸣", "text": "Aion批注"}], ensure_ascii=False), "Aion本段感想", 1.0, "aion"),
            )
            conn.execute(
                "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                ("b1", 0, 1, json.dumps([{"p": 4, "type": "分析", "text": "Aion下段批注"}], ensure_ascii=False), "Aion下段感想", 1.0, "aion"),
            )
            conn.execute(
                "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                ("b1", 0, 0, json.dumps([{"p": 2, "type": "吐槽", "text": "Connor批注"}], ensure_ascii=False), "Connor本段感想", 1.0, "connor"),
            )
            conn.commit()
            conn.close()

            db_factory = lambda: aiosqlite.connect(db_path)

            aion_ctx = await book.load_reading_selection_context(
                {"book_id": "b1", "chapter_index": 0, "chapter_id": 101, "selected_text": "窗台上落了很轻的一层雪"},
                actor="aion",
                db_factory=db_factory,
            )
            self.assertIn("当前共读：《冬夜手记》｜雪落窗前", aion_ctx)
            self.assertNotIn("未知作者", aion_ctx)
            self.assertIn("第二段：前情铺垫B。", aion_ctx)
            self.assertNotIn("第一段：前情铺垫A。", aion_ctx)  # 超过 1200 字预算时只保留紧邻上文
            self.assertIn("【选中所在段落】\n第三段：窗台上落了很轻的一层雪。", aion_ctx)
            self.assertIn("第四段：他把信纸折好放进口袋。", aion_ctx)
            self.assertIn("Aion本段感想", aion_ctx)
            self.assertIn("共鸣：Aion批注", aion_ctx)
            self.assertNotIn("Aion下段感想", aion_ctx)
            self.assertNotIn("Connor本段感想", aion_ctx)

            connor_ctx = await book.load_reading_selection_context(
                book.ReadingContextRef(book_id="b1", chapter_index=0, chapter_id=101, start_p=2, end_p=2),
                actor="connor",
                db_factory=db_factory,
            )
            self.assertIn("Connor本段感想", connor_ctx)
            self.assertIn("吐槽：Connor批注", connor_ctx)
            self.assertNotIn("Aion本段感想", connor_ctx)

            self.assertEqual(
                await book.load_reading_selection_context(
                    {"book_id": "b1", "chapter_index": 0, "chapter_id": 999, "start_p": 2, "end_p": 2},
                    actor="aion",
                    db_factory=db_factory,
                ),
                "",
            )
            self.assertEqual(
                await book.load_reading_selection_context(
                    {"book_id": "missing", "chapter_index": 0, "start_p": 0, "end_p": 0},
                    actor="aion",
                    db_factory=db_factory,
                ),
                "",
            )

    async def test_chatroom_group_and_connor_1v1_inject_reading_context_without_altering_saved_msg(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "books.db"
            _create_books_db(db_path)
            paragraphs = ["上文：雨停在巷口。", "选中：灯笼在风里晃了三下。", "下文：门后有人应了一声。"]
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("b2", "巷口灯火", "作者甲", None, 1, 0, 0, 1.0))
            conn.execute(
                "INSERT INTO book_chapters (id, book_id, chapter_index, title, html_content, text_content, paragraphs, char_count, segment_count, segments_meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (202, "b2", 0, "第一章", "", "", json.dumps(paragraphs, ensure_ascii=False), 40, 1, json.dumps([{"start_p": 0, "end_p": 2}])),
            )
            conn.execute(
                "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                ("b2", 0, 0, json.dumps([{"p": 1, "type": "感悟", "text": "Aion注意到的光影"}]), "Aion感想", 1.0, "aion"),
            )
            conn.execute(
                "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                ("b2", 0, 0, json.dumps([{"p": 1, "type": "分析", "text": "Connor注意到的伏笔"}]), "Connor感想", 1.0, "connor"),
            )
            conn.commit()
            conn.close()

            real_loader = book.load_reading_selection_context

            async def load_from_test_db(ref, *, actor=None):
                return await real_loader(ref, actor=actor, db_factory=lambda: aiosqlite.connect(db_path))

            saved_contents = []
            aion_prompts = []
            connor_prompts = []

            async def fake_save_msg(room_id, sender, content, msg_id=None, **kwargs):
                msg = {"id": msg_id or "m1", "room_id": room_id, "sender": sender, "content": content, "attachments": [], "duplicate": False}
                if sender == "user":
                    saved_contents.append(content)
                return msg

            async def fake_stream_ai(prompts, *args, **kwargs):
                aion_prompts.append(prompts)
                yield "Aion回复"

            async def fake_stream_connor(prompts, *args, **kwargs):
                connor_prompts.append(prompts)
                yield "Connor回复"

            async def fake_kitchen_stream(prompts, stream_factory, **kwargs):
                async for chunk in stream_factory(prompts):
                    yield chunk

            ref = book.ReadingContextRef(
                book_id="b2",
                chapter_index=0,
                chapter_id=202,
                start_p=1,
                end_p=1,
                selected_text="灯笼在风里晃了三下。",
            )
            user_text = "我在读《巷口灯火》「第一章」，看到这段话：\n「灯笼在风里晃了三下。」\n你们怎么看？"

            patches = [
                patch("kitchen.enabled", return_value=False),
                patch.object(chatroom_routes, "load_reading_selection_context", side_effect=load_from_test_db),
                patch.object(chatroom_routes, "validate_send", new=AsyncMock()),
                patch.object(chatroom_routes, "reserve_message_ingress", new=AsyncMock(return_value=None)),
                patch.object(chatroom_routes, "attach_to_message", new=AsyncMock(side_effect=lambda _t, _id, msg, _r: (msg, None))),
                patch.object(chatroom_routes, "record_chatroom_active", new=AsyncMock()),
                patch.object(chatroom_routes, "get_chatroom_names", return_value=("用户", "Aion", "Connor")),
                patch.object(chatroom_routes, "load_chatroom_config", return_value={"reply_order": "aion"}),
                patch.object(chatroom_routes, "_is_manual_group_reply_mode", return_value=False),
                patch.object(chatroom_routes, "_save_msg", new=fake_save_msg),
                patch.object(chatroom_routes, "instant_digest", new=AsyncMock(return_value={"first_responder": "aion", "keywords": [], "topic": "", "is_search_needed": False, "require_detail": False, "status": ""})),
                patch.object(chatroom_routes, "build_aion_group_context", new=AsyncMock(return_value=([{"role": "user", "content": user_text}], {}))),
                patch.object(chatroom_routes, "build_connor_group_context", new=AsyncMock(return_value=([{"role": "user", "content": user_text}], {}))),
                patch.object(chatroom_routes, "build_connor_1v1_context", new=AsyncMock(return_value=([{"role": "user", "content": user_text}], {}))),
                patch.object(chatroom_routes, "stream_ai", new=fake_stream_ai),
                patch.object(chatroom_routes, "_stream_connor_model", new=fake_stream_connor),
                patch.object(chatroom_routes, "kitchen_stream", new=fake_kitchen_stream),
                patch.object(chatroom_routes, "finish_kitchen_reply", new=AsyncMock(side_effect=lambda text, **k: (text, []))),
                patch.object(chatroom_routes, "_process_chatroom_commands", new=AsyncMock(side_effect=lambda text, *a, **k: (text, {}))),
                patch.object(chatroom_routes, "_extract_and_save_images", new=AsyncMock(return_value=[])),
                patch.object(chatroom_routes, "_emit_chatroom_debug", new=AsyncMock()),
                patch.object(chatroom_routes, "_fire_chatroom_followups"),
                patch.object(chatroom_routes.cam, "reset_patrol_timer"),
                patch.object(chatroom_routes, "broadcast_synced", new=AsyncMock()),
            ]

            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                with patch.object(
                    chatroom_routes,
                    "_load_room_and_messages",
                    new=AsyncMock(return_value=({"id": "g1", "type": "group", "context_minutes": 30}, [{"sender": "user", "content": user_text}])),
                ):
                    resp = await chatroom_routes.send_message(
                        "g1",
                        chatroom_routes.MsgSend(content=user_text, reading_ref=ref),
                    )
                    async for _ in resp.body_iterator:
                        pass

                with patch.object(
                    chatroom_routes,
                    "_load_room_and_messages",
                    new=AsyncMock(return_value=({"id": "c1", "type": "connor_1v1", "context_minutes": 30}, [{"sender": "user", "content": user_text}])),
                ):
                    resp_1v1 = await chatroom_routes.send_message(
                        "c1",
                        chatroom_routes.MsgSend(content=user_text, reading_ref=ref),
                    )
                    async for _ in resp_1v1.body_iterator:
                        pass

            self.assertEqual(saved_contents, [user_text, user_text])
            self.assertEqual(len(aion_prompts), 1)
            aion_blob = "\n".join(str(m.get("content", "")) for m in aion_prompts[0])
            self.assertIn("上文：雨停在巷口。", aion_blob)
            self.assertIn("下文：门后有人应了一声。", aion_blob)
            self.assertIn("Aion感想", aion_blob)
            self.assertIn("读书摘录", aion_blob)
            self.assertNotIn("Connor感想", aion_blob)

            self.assertEqual(len(connor_prompts), 2)
            for prompts in connor_prompts:
                blob = "\n".join(str(m.get("content", "")) for m in prompts)
                self.assertIn("上文：雨停在巷口。", blob)
                self.assertIn("下文：门后有人应了一声。", blob)
                self.assertIn("Connor感想", blob)
                self.assertIn("读书摘录", blob)
                self.assertNotIn("Aion感想", blob)

    async def test_private_chat_send_injects_reading_context_in_normal_and_fast_modes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "private.db"
            old_db_path = database.DB_PATH
            database.DB_PATH = db_path
            try:
                await database.init_db()
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_1", "私聊", "unit-model", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("b3", "海风集", "作者乙", None, 1, 0, 0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO book_chapters (id, book_id, chapter_index, title, html_content, text_content, paragraphs, char_count, segment_count, segments_meta) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (303, "b3", 0, "灯塔", "", "", json.dumps(["上文：潮水退去。", "选中：灯塔熄了灯。", "下文：船铃响了一声。"], ensure_ascii=False), 30, 1, json.dumps([{"start_p": 0, "end_p": 2}])),
                    )
                    await db.execute(
                        "INSERT INTO book_annotations (book_id, chapter_index, segment_index, annotations, summary, created_at, annotator) VALUES (?,?,?,?,?,?,?)",
                        ("b3", 0, 0, json.dumps([{"p": 1, "type": "共鸣", "text": "很安静"}]), "Aion私聊读后感", 1.0, "aion"),
                    )
                    await db.commit()

                captured = []

                async def fake_stream_ai(messages, *args, **kwargs):
                    captured.append(list(messages))
                    yield "收到了。"

                async def fake_kitchen_stream(messages, stream_factory, **kwargs):
                    async for chunk in stream_factory(messages):
                        yield chunk

                user_text = "我在读《海风集》「灯塔」，看到这段话：\n「灯塔熄了灯。」\n这句好安静"
                ref = book.ReadingContextRef(
                    book_id="b3",
                    chapter_index=0,
                    chapter_id=303,
                    start_p=1,
                    end_p=1,
                    selected_text="灯塔熄了灯。",
                )
                rendered_history = [{"role": "user", "content": user_text, "attachments": []}]

                patches = [
                    patch("kitchen.enabled", return_value=False),
                    patch("routes.chat.validate_send", new=AsyncMock()),
                    patch("routes.chat.attach_to_message", new=AsyncMock(side_effect=lambda _t, _id, msg, _r: (msg, None))),
                    patch("routes.chat.record_aion_private_active", new=AsyncMock()),
                    patch("routes.chat.resolve_model_key", return_value="unit-model"),
                    patch("routes.chat.pick_random_model_from_pool", return_value=None),
                    patch("routes.chat.fetch_merged_timeline", new=AsyncMock(return_value=[{"created_at": 10.0}])),
                    patch("routes.chat.render_merged_timeline", side_effect=lambda *a, **k: [dict(m) for m in rendered_history]),
                    patch("routes.chat.load_worldbook", return_value={}),
                    patch("routes.chat._insert_private_ability_block", new=AsyncMock(return_value=0)),
                    patch("routes.chat.instant_digest", new=AsyncMock(return_value={"keywords": ["灯塔"], "topic": "读书", "is_search_needed": False, "require_detail": False, "status": ""})),
                    patch("routes.chat.build_health_summary", new=AsyncMock(return_value="")),
                    patch("routes.chat.build_surfacing_memories", new=AsyncMock(return_value=([], set()))),
                    patch("routes.chat.recall_memories", new=AsyncMock(return_value=([{"id": "m1", "type": "event", "content": "之前去过海边", "score": 0.9}], []))),
                    patch("routes.chat.fetch_source_details", new=AsyncMock(return_value="")),
                    patch("routes.chat.stream_ai", new=fake_stream_ai),
                    patch("routes.chat.kitchen_stream", new=fake_kitchen_stream),
                    patch("routes.chat.process_schedule_commands", new=AsyncMock(side_effect=lambda text, *a, **k: text)),
                    patch("routes.chat._process_home_commands", new=AsyncMock(side_effect=lambda text: text)),
                    patch("routes.chat.handle_luckin_commands", new=AsyncMock(side_effect=lambda text: (text, []))),
                    patch("routes.chat._process_wish_commands", new=AsyncMock(side_effect=lambda text, **k: text)),
                    patch("routes.chat._extract_reply_image_attachments", side_effect=lambda text: (text, [])),
                    patch("routes.chat.luckin_payment_attachments", return_value=[]),
                    patch("routes.chat.with_band_vibration_attachment", new=AsyncMock(side_effect=lambda message_id, attachments: attachments)),
                    patch("routes.chat.export_conversation", new=AsyncMock()),
                    patch.object(chat_routes.manager, "broadcast", new=AsyncMock()),
                    patch.object(chat_routes.manager, "set_tts_fallback", new=Mock()),
                    patch.object(chat_routes.cam, "reset_patrol_timer"),
                ]

                with ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    for fast_mode in (False, True):
                        resp = await chat_routes.send_message(
                            "conv_1",
                            chat_routes.MsgCreate(
                                content=user_text if not fast_mode else user_text + "（追问）",
                                fast_mode=fast_mode,
                                reading_ref=ref,
                            ),
                        )
                        async for _ in resp.body_iterator:
                            pass

                self.assertEqual(len(captured), 2)
                for messages in captured:
                    contents = [str(m.get("content", "")) for m in messages]
                    self.assertTrue(any("[当前共读上下文]" in c for c in contents))
                    joined = "\n".join(contents)
                    self.assertIn("上文：潮水退去。", joined)
                    self.assertIn("下文：船铃响了一声。", joined)
                    self.assertIn("Aion私聊读后感", joined)
                    self.assertEqual(messages[-1]["content"], user_text)

                normal_contents = [str(m.get("content", "")) for m in captured[0]]
                mem_idx = next(i for i, c in enumerate(normal_contents) if "[相关记忆]" in c)
                read_idx = next(i for i, c in enumerate(normal_contents) if "[当前共读上下文]" in c)
                self.assertLess(mem_idx, read_idx)

                async with aiosqlite.connect(db_path) as db:
                    rows = await (await db.execute("SELECT content FROM messages WHERE conv_id='conv_1' AND role='user' ORDER BY created_at")).fetchall()
                self.assertEqual(rows[0][0], user_text)
                self.assertNotIn("【上文】", rows[0][0])
            finally:
                database.DB_PATH = old_db_path


if __name__ == "__main__":
    unittest.main()
