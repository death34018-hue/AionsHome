import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


class DateTheaterAssetTests(unittest.TestCase):
    def test_scan_date_assets_groups_backgrounds_and_state_videos(self):
        from date_theater import scan_date_assets

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "透明视频").mkdir()
            (root / "背景-客厅.png").write_bytes(b"png")
            (root / "背景-书房.png").write_bytes(b"png")
            (root / "透明视频" / "平静.webm").write_bytes(b"webm")
            (root / "悠闲.mp4").write_bytes(b"mp4")

            assets = scan_date_assets(root, public_prefix="/public/去约会小剧场素材")

        self.assertEqual(assets["default_background"], "背景-客厅")
        self.assertEqual(assets["default_state"], "平静")
        self.assertEqual([item["id"] for item in assets["backgrounds"]], ["背景-客厅", "背景-书房"])
        states = {item["id"]: item for item in assets["states"]}
        self.assertEqual(states["平静"]["kind"], "transparent")
        self.assertEqual(states["平静"]["mime"], "video/webm")
        self.assertEqual(states["悠闲"]["kind"], "scene")
        self.assertEqual(states["悠闲"]["mime"], "video/mp4")

    def test_extract_stage_commands_strips_control_tags(self):
        from date_theater import extract_stage_commands

        visible, commands = extract_stage_commands(
            "今晚先坐近一点。\n[DATE_BACKGROUND:背景-阳台]\n[DATE_STATE:过来]\n[MUSIC:告白气球]\n[DATE_END_READY]"
        )

        self.assertEqual(visible, "今晚先坐近一点。")
        self.assertEqual(commands["background"], "背景-阳台")
        self.assertEqual(commands["state"], "过来")
        self.assertEqual(commands["music"], ["告白气球"])
        self.assertTrue(commands["end_ready"])

    def test_build_start_prompt_mentions_assets_and_not_prior_chat(self):
        from date_theater import build_start_prompt

        assets = {
            "backgrounds": [{"id": "背景-客厅"}, {"id": "背景-阳台"}],
            "states": [{"id": "平静"}, {"id": "过来"}, {"id": "暧昧"}],
        }
        prompt = build_start_prompt(
            partner_name="AI",
            user_name="用户",
            persona="温柔、克制、会主动推进约会。",
            assets=assets,
        )

        self.assertIn("生成一个约会标题", prompt)
        self.assertIn("结束契机", prompt)
        self.assertIn("背景-客厅", prompt)
        self.assertIn("过来", prompt)
        self.assertNotIn("最近聊天", prompt)
        self.assertNotIn("历史上下文", prompt)

    def test_build_outline_prompt_uses_user_prompt_and_selected_persona(self):
        from date_theater import build_outline_prompt

        assets = {
            "backgrounds": [{"id": "背景-客厅"}, {"id": "背景-雨夜"}],
            "states": [{"id": "平静"}, {"id": "暧昧"}],
        }
        prompt = build_outline_prompt(
            partner_name="约会对象",
            user_name="用户",
            persona="慢热、克制、会在雨声里推进情绪。",
            user_prompt="想要雨夜、旧唱片、重逢感。",
            assets=assets,
        )

        self.assertIn("想要雨夜、旧唱片、重逢感。", prompt)
        self.assertIn("慢热、克制、会在雨声里推进情绪。", prompt)
        self.assertIn("不要直接开始正式约会", prompt)
        self.assertIn('"outline"', prompt)
        self.assertIn("背景-雨夜", prompt)
        self.assertIn("暧昧", prompt)

    def test_parse_outline_payload_keeps_outline_and_opening_separate(self):
        from date_theater import parse_outline_payload

        assets = {
            "backgrounds": [{"id": "背景-客厅"}, {"id": "背景-雨夜"}],
            "states": [{"id": "平静"}, {"id": "暧昧"}],
            "default_background": "背景-客厅",
            "default_state": "平静",
        }
        payload = parse_outline_payload(
            '{"title":"雨夜重逢","outline":"先重逢，再靠近，最后在停雨时收束。","opening":"你听见雨声了吗？","ending_trigger":"雨停时结束","background":"背景-雨夜","state":"暧昧"}',
            assets,
        )

        self.assertEqual(payload["title"], "雨夜重逢")
        self.assertEqual(payload["outline"], "先重逢，再靠近，最后在停雨时收束。")
        self.assertEqual(payload["opening"], "你听见雨声了吗？")
        self.assertEqual(payload["ending_trigger"], "雨停时结束")
        self.assertEqual(payload["background"], "背景-雨夜")
        self.assertEqual(payload["state"], "暧昧")

    def test_build_sync_message_uses_only_title_and_summary(self):
        from date_theater import build_sync_card_attachment, build_sync_message

        message = build_sync_message("雨夜情迷", "雨声里，他把话说得很轻，也终于把想念说完整。")
        attachment = build_sync_card_attachment("雨夜情迷", "雨声里，他把话说得很轻，也终于把想念说完整。")

        self.assertEqual(
            message,
            "【刚刚完成了约会：雨夜情迷】\n雨声里，他把话说得很轻，也终于把想念说完整。",
        )
        self.assertEqual(
            attachment,
            {"type": "date_summary", "title": "雨夜情迷", "summary": "雨声里，他把话说得很轻，也终于把想念说完整。"},
        )

    def test_tts_strips_date_stage_tags(self):
        from tts import split_text_for_tts

        parts = split_text_for_tts(
            "靠近一点。[DATE_STATE : 过来][DATE_BACKGROUND:背景-阳台]",
            min_chars=1,
            max_chars=200,
        )

        self.assertEqual(parts, ["靠近一点。"])

    def test_list_date_message_tts_urls_prefers_merged_then_sorted_segments(self):
        from date_theater import list_date_message_tts_urls

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "date_ai_1.mp3").write_bytes(b"merged")
            (cache_dir / "date_ai_1_s0.mp3").write_bytes(b"segment")
            (cache_dir / "date_ai_2_s10.mp3").write_bytes(b"segment-10")
            (cache_dir / "date_ai_2_s2.mp3").write_bytes(b"segment-2")
            (cache_dir / "date_ai_2_s0.mp3").write_bytes(b"segment-0")

            merged_urls = list_date_message_tts_urls(
                "date_ai_1",
                cache_dir=cache_dir,
                audio_url_prefix="/audio",
            )
            segment_urls = list_date_message_tts_urls(
                "date_ai_2",
                cache_dir=cache_dir,
                audio_url_prefix="/audio",
            )

        self.assertEqual(merged_urls, ["/audio/date_ai_1"])
        self.assertEqual(segment_urls, ["/audio/date_ai_2_s0", "/audio/date_ai_2_s2", "/audio/date_ai_2_s10"])

    def test_date_message_tts_route_exposes_replay_audio_urls(self):
        from routes import date_theater as route

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "date_ai_3_s1.mp3").write_bytes(b"segment-1")
            (cache_dir / "date_ai_3_s0.mp3").write_bytes(b"segment-0")
            old_cache_dir = route.DATE_THEATER_TTS_CACHE_DIR
            try:
                route.DATE_THEATER_TTS_CACHE_DIR = cache_dir
                data = asyncio.run(route.date_message_tts_urls("date_ai_3"))
            finally:
                route.DATE_THEATER_TTS_CACHE_DIR = old_cache_dir

        self.assertEqual(data, {
            "urls": [
                "/api/date-theater/tts/audio/date_ai_3_s0",
                "/api/date-theater/tts/audio/date_ai_3_s1",
            ]
        })

    def test_legacy_system_date_summary_is_kept_in_chat_context(self):
        from context_builder import render_merged_timeline

        history = render_merged_timeline(
            [{
                "source": "group",
                "sender": "system",
                "content": "【刚刚完成了约会：雨夜情迷】\n短摘要",
                "created_at": 1.0,
                "attachments": "[]",
            }],
            "aion",
        )

        self.assertTrue(any("刚刚完成了约会" in item["content"] for item in history))

    def test_resolve_date_model_ignores_invalid_legacy_default(self):
        from date_theater import resolve_date_model

        model = resolve_date_model(
            requested="",
            cfg={"model": "Gemini-3.5-flash"},
            chatroom_cfg={"aion_model": "room-model"},
            model_keys=["room-model", "other-model"],
        )

        self.assertEqual(model, "room-model")

    def test_resolve_date_model_prefers_chatroom_until_model_locked(self):
        from date_theater import resolve_date_model

        unlocked = resolve_date_model(
            requested="",
            cfg={"model": "date-model", "model_locked": False},
            chatroom_cfg={"aion_model": "room-model"},
            model_keys=["date-model", "room-model"],
        )
        locked = resolve_date_model(
            requested="",
            cfg={"model": "date-model", "model_locked": True},
            chatroom_cfg={"aion_model": "room-model"},
            model_keys=["date-model", "room-model"],
        )

        self.assertEqual(unlocked, "room-model")
        self.assertEqual(locked, "date-model")

    def test_normalize_persona_presets_keeps_active_persona(self):
        from date_theater import normalize_persona_presets

        cfg = normalize_persona_presets({
            "partner_name": "旧称呼",
            "persona": "legacy persona",
            "active_persona_id": "bold",
            "persona_presets": [
                {"id": "soft", "name": "Soft", "persona": "soft persona"},
                {"id": "bold", "name": "Bold", "persona": "bold persona"},
            ],
        })

        self.assertEqual(cfg["active_persona_id"], "bold")
        self.assertEqual(cfg["persona"], "bold persona")
        self.assertEqual(cfg["partner_name"], "Bold")
        self.assertEqual([item["id"] for item in cfg["persona_presets"]], ["soft", "bold"])

    def test_save_date_config_can_keep_chatroom_model_unlocked(self):
        import date_theater

        old_path = date_theater.DATE_CONFIG_PATH
        with tempfile.TemporaryDirectory() as td:
            date_theater.DATE_CONFIG_PATH = Path(td) / "date_theater_config.json"
            try:
                cfg = date_theater.save_date_config({"model": "room-model", "model_locked": False})
                saved = json.loads(date_theater.DATE_CONFIG_PATH.read_text(encoding="utf-8"))
            finally:
                date_theater.DATE_CONFIG_PATH = old_path

        self.assertEqual(cfg["model"], "room-model")
        self.assertFalse(cfg["model_locked"])
        self.assertFalse(saved["model_locked"])


class DateTheaterDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_creates_independent_date_tables(self):
        import database

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            database.DB_PATH = Path(td) / "chat.db"
            try:
                await database.init_db()
                con = sqlite3.connect(database.DB_PATH)
                try:
                    rows = con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'date_%' ORDER BY name"
                    ).fetchall()
                    session_cols = {row[1] for row in con.execute("PRAGMA table_info(date_sessions)").fetchall()}
                finally:
                    con.close()
            finally:
                database.DB_PATH = old_path

        self.assertEqual([row[0] for row in rows], ["date_messages", "date_sessions"])
        self.assertTrue({"prompt", "outline", "opening", "partner_name", "outline_usage", "end_usage"}.issubset(session_cols))

    async def test_sync_helpers_choose_latest_chat_window_and_insert_user_card_message(self):
        import aiosqlite
        import database
        from date_theater import build_sync_card_attachment, build_sync_message, find_last_chat_target, insert_sync_message

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            database.DB_PATH = Path(td) / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_old", "私聊", "m", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_old", "conv_old", "user", "旧私聊", 1.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_new", "最近群聊", "group", 2.0, 2.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("cr_new", "room_new", "user", "最近群聊消息", 2.0, "[]"),
                    )
                    await db.commit()

                    target = await find_last_chat_target(db, private_label="AI 私聊")
                    self.assertEqual(target["type"], "chatroom")
                    self.assertEqual(target["id"], "room_new")
                    self.assertEqual(target["label"], "最近群聊")

                    msg = await insert_sync_message(
                        db,
                        target,
                        build_sync_message("雨夜情迷", "短摘要"),
                        attachment=build_sync_card_attachment("雨夜情迷", "短摘要"),
                        now=3.0,
                    )
                    await db.commit()
                    cur = await db.execute(
                        "SELECT sender, content, attachments FROM chatroom_messages WHERE id=?",
                        (msg["id"],),
                    )
                    row = await cur.fetchone()
            finally:
                database.DB_PATH = old_path

        self.assertEqual(row["sender"], "user")
        self.assertEqual(row["content"], "【刚刚完成了约会：雨夜情迷】\n短摘要")
        self.assertEqual(json.loads(row["attachments"]), [{"type": "date_summary", "title": "雨夜情迷", "summary": "短摘要"}])

    async def test_sync_helpers_list_targets_and_insert_to_selected_window(self):
        import aiosqlite
        import database
        from date_theater import build_sync_card_attachment, build_sync_message, insert_sync_message, list_chat_targets, resolve_sync_target

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            database.DB_PATH = Path(td) / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_old", "旧私聊", "m", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_old", "conv_old", "user", "旧私聊消息", 1.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_empty", "空私聊", "m", 3.0, 3.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_new", "最近群聊", "group", 2.0, 2.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("cr_new", "room_new", "user", "最近群聊消息", 2.0, "[]"),
                    )
                    await db.commit()

                    targets = await list_chat_targets(db, private_label="AI 私聊")
                    by_key = {f"{item['type']}:{item.get('id') or ''}": item for item in targets}

                    self.assertIn("private:conv_old", by_key)
                    self.assertIn("private:conv_empty", by_key)
                    self.assertIn("chatroom:room_new", by_key)
                    self.assertEqual(by_key["private:conv_empty"]["label"], "空私聊")
                    self.assertTrue(by_key["chatroom:room_new"]["is_default"])
                    self.assertFalse(by_key["private:conv_empty"]["is_default"])

                    target = await resolve_sync_target(db, "private", "conv_old", private_label="AI 私聊")
                    msg = await insert_sync_message(
                        db,
                        target,
                        build_sync_message("雨夜情迷", "短摘要"),
                        attachment=build_sync_card_attachment("雨夜情迷", "短摘要"),
                        now=4.0,
                    )
                    await db.commit()
                    cur = await db.execute(
                        "SELECT role, content, attachments FROM messages WHERE id=? AND conv_id='conv_old'",
                        (msg["id"],),
                    )
                    row = await cur.fetchone()
            finally:
                database.DB_PATH = old_path

        self.assertEqual(row["role"], "user")
        self.assertEqual(row["content"], "【刚刚完成了约会：雨夜情迷】\n短摘要")
        self.assertEqual(json.loads(row["attachments"]), [{"type": "date_summary", "title": "雨夜情迷", "summary": "短摘要"}])

    async def test_sync_helpers_list_three_summary_targets(self):
        import aiosqlite
        import database
        from date_theater import list_summary_sync_targets

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            database.DB_PATH = Path(td) / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_main", "Alpha private", "m", 1.0, 8.0),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_main", "conv_main", "assistant", "latest main", 8.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_group", "latest group", "group", 2.0, 10.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_group", "room_group", "user", "latest group", 10.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_connor", "Beta private", "connor_1v1", 3.0, 6.0),
                    )
                    await db.commit()

                    targets = await list_summary_sync_targets(
                        db,
                        private_label="Alpha private",
                        ai_name="Alpha",
                        connor_name="Beta",
                    )
            finally:
                database.DB_PATH = old_path

        self.assertEqual([(item["type"], item["id"]) for item in targets], [
            ("group", "latest"),
            ("aion", "latest"),
            ("connor", "latest"),
        ])
        self.assertEqual([item["label"] for item in targets], ["同步到群聊", "同步给【Alpha】", "同步给【Beta】"])
        self.assertEqual([item["type"] for item in targets if item.get("is_default")], ["group"])

    async def test_semantic_sync_targets_resolve_to_latest_window_for_each_scope(self):
        import aiosqlite
        import database
        from date_theater import build_sync_card_attachment, build_sync_message, insert_sync_message, resolve_sync_target

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            database.DB_PATH = Path(td) / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_old", "old private", "m", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("conv_latest", "latest private", "m", 2.0, 30.0),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_old", "conv_old", "user", "old", 1.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_latest", "conv_latest", "assistant", "latest", 9.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("msg_system_noise", "conv_old", "system", "system should not win", 99.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_old_group", "old group", "group", 3.0, 90.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_latest_group", "latest group", "group", 4.0, 4.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("group_old_system", "room_old_group", "system", "system should not win", 95.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("group_latest", "room_latest_group", "aion", "latest group", 12.0, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_connor_old", "old Beta", "connor_1v1", 5.0, 5.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_rooms (id, title, type, created_at, updated_at) VALUES (?,?,?,?,?)",
                        ("room_connor_latest", "latest Beta", "connor_1v1", 6.0, 6.0),
                    )
                    await db.execute(
                        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("connor_latest", "room_connor_latest", "connor", "latest connor", 11.0, "[]"),
                    )
                    await db.commit()

                    group_target = await resolve_sync_target(db, "group", "latest", private_label="Alpha private", ai_name="Alpha", connor_name="Beta")
                    aion_target = await resolve_sync_target(db, "aion", "latest", private_label="Alpha private", ai_name="Alpha", connor_name="Beta")
                    connor_target = await resolve_sync_target(db, "connor", "latest", private_label="Alpha private", ai_name="Alpha", connor_name="Beta")

                    self.assertEqual((group_target["type"], group_target["id"]), ("chatroom", "room_latest_group"))
                    self.assertEqual((aion_target["type"], aion_target["id"]), ("private", "conv_latest"))
                    self.assertEqual((connor_target["type"], connor_target["id"]), ("chatroom", "room_connor_latest"))

                    msg = await insert_sync_message(
                        db,
                        connor_target,
                        build_sync_message("Rain", "Short summary"),
                        attachment=build_sync_card_attachment("Rain", "Short summary"),
                        now=13.0,
                    )
                    await db.commit()
                    cur = await db.execute(
                        "SELECT sender, content, attachments FROM chatroom_messages WHERE id=? AND room_id='room_connor_latest'",
                        (msg["id"],),
                    )
                    row = await cur.fetchone()
            finally:
                database.DB_PATH = old_path

        self.assertEqual(row["sender"], "user")
        self.assertEqual(row["content"], "【刚刚完成了约会：Rain】\nShort summary")
        self.assertEqual(json.loads(row["attachments"]), [{"type": "date_summary", "title": "Rain", "summary": "Short summary"}])

    async def test_delete_date_session_removes_messages_and_cached_tts_audio(self):
        import aiosqlite
        import database
        from date_theater import delete_date_session

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_dir = root / "date_tts"
            cache_dir.mkdir()
            (cache_dir / "date_ai_1.mp3").write_bytes(b"audio-1")
            (cache_dir / "date_ai_2.mp3").write_bytes(b"audio-2")
            (cache_dir / "other.mp3").write_bytes(b"keep")
            database.DB_PATH = root / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO date_sessions (id, title, status, model, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                        ("date_1", "雨夜情迷", "active", "m", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO date_messages (id, session_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("date_ai_1", "date_1", "assistant", "第一句", 1.1, "[]"),
                    )
                    await db.execute(
                        "INSERT INTO date_messages (id, session_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        ("date_ai_2", "date_1", "assistant", "第二句", 1.2, "[]"),
                    )
                    await db.commit()

                    deleted = await delete_date_session(db, "date_1", cache_dir=cache_dir)
                    await db.commit()
                    cur = await db.execute("SELECT COUNT(*) FROM date_sessions WHERE id='date_1'")
                    session_count = (await cur.fetchone())[0]
                    cur = await db.execute("SELECT COUNT(*) FROM date_messages WHERE session_id='date_1'")
                    message_count = (await cur.fetchone())[0]
                    audio_1_exists = (cache_dir / "date_ai_1.mp3").exists()
                    audio_2_exists = (cache_dir / "date_ai_2.mp3").exists()
                    other_exists = (cache_dir / "other.mp3").exists()
            finally:
                database.DB_PATH = old_path

        self.assertTrue(deleted)
        self.assertEqual(session_count, 0)
        self.assertEqual(message_count, 0)
        self.assertFalse(audio_1_exists)
        self.assertFalse(audio_2_exists)
        self.assertTrue(other_exists)

    async def test_delete_date_message_removes_audio_and_rewinds_stage_to_previous_message(self):
        import aiosqlite
        import database
        from date_theater import delete_date_message

        old_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_dir = root / "date_tts"
            cache_dir.mkdir()
            (cache_dir / "date_ai_bad.mp3").write_bytes(b"merged")
            (cache_dir / "date_ai_bad_s0.mp3").write_bytes(b"segment")
            (cache_dir / "date_ai_good.mp3").write_bytes(b"keep")
            database.DB_PATH = root / "chat.db"
            try:
                await database.init_db()
                async with aiosqlite.connect(database.DB_PATH) as db:
                    await db.execute(
                        """
                        INSERT INTO date_sessions
                        (id, title, status, model, current_background, current_state, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        ("date_1", "雨夜情迷", "active", "m", "背景-坏", "坏动作", 1.0, 1.0),
                    )
                    await db.execute(
                        "INSERT INTO date_messages (id, session_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (
                            "date_ai_good",
                            "date_1",
                            "assistant",
                            "上一条不错",
                            1.1,
                            json.dumps([{"type": "date_stage", "background": "背景-好", "state": "好动作"}], ensure_ascii=False),
                        ),
                    )
                    await db.execute(
                        "INSERT INTO date_messages (id, session_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (
                            "date_ai_bad",
                            "date_1",
                            "assistant",
                            "这条不满意",
                            1.2,
                            json.dumps([{"type": "date_stage", "background": "背景-坏", "state": "坏动作"}], ensure_ascii=False),
                        ),
                    )
                    await db.commit()

                    deleted = await delete_date_message(
                        db,
                        "date_ai_bad",
                        cache_dir=cache_dir,
                        default_background="背景-客厅",
                        default_state="平静",
                    )
                    await db.commit()
                    cur = await db.execute("SELECT COUNT(*) FROM date_messages WHERE id='date_ai_bad'")
                    bad_count = (await cur.fetchone())[0]
                    cur = await db.execute(
                        "SELECT current_background, current_state FROM date_sessions WHERE id='date_1'"
                    )
                    session_row = await cur.fetchone()
                    bad_audio_exists = (cache_dir / "date_ai_bad.mp3").exists()
                    bad_segment_exists = (cache_dir / "date_ai_bad_s0.mp3").exists()
                    good_audio_exists = (cache_dir / "date_ai_good.mp3").exists()
            finally:
                database.DB_PATH = old_path

        self.assertTrue(deleted)
        self.assertEqual(bad_count, 0)
        self.assertEqual(session_row[0], "背景-好")
        self.assertEqual(session_row[1], "好动作")
        self.assertFalse(bad_audio_exists)
        self.assertFalse(bad_segment_exists)
        self.assertTrue(good_audio_exists)


if __name__ == "__main__":
    unittest.main()
