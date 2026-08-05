"""
数据库初始化与连接
"""

import aiosqlite
from schedule_history import migrate_schedule_history
from config import DB_PATH
from message_dedup import ensure_message_ingress_dedupe_table


async def _table_has_rows(db, table_name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    if await cursor.fetchone() is None:
        return False
    cursor = await db.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
    return await cursor.fetchone() is not None


async def _bootstrap_english_corner_schema(
    db,
    *,
    settings=None,
    persist_settings=None,
) -> bool:
    cursor = await db.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'english_learning_packs'
        """
    )
    schema_already_existed = await cursor.fetchone() is not None

    from english_corner import ensure_english_corner_tables

    await ensure_english_corner_tables(db)
    if schema_already_existed:
        return False

    if settings is None or persist_settings is None:
        from config import SETTINGS, save_settings

        if settings is None:
            settings = SETTINGS
        if persist_settings is None:
            persist_settings = save_settings

    capability_settings = settings.get("ai_prompt_capabilities")
    existing_capabilities = (
        capability_settings if isinstance(capability_settings, dict) else {}
    )
    if "english_corner_reminder" in existing_capabilities:
        return False

    has_existing_history = await _table_has_rows(db, "conversations")
    if not has_existing_history:
        has_existing_history = await _table_has_rows(db, "chatroom_messages")
    if not has_existing_history:
        return False

    updated_capabilities = dict(existing_capabilities)
    updated_capabilities["english_corner_reminder"] = True
    settings["ai_prompt_capabilities"] = updated_capabilities
    persist_settings(settings)
    return True


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT ''")
        except:
            pass
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN starred INTEGER DEFAULT 0")
        except:
            pass
        for col, defn in [
            ("reasoning_content", "TEXT DEFAULT ''"),
            ("ai_feedback_rating", "TEXT DEFAULT ''"),
            ("ai_feedback_reason", "TEXT DEFAULT ''"),
            ("ai_feedback_created_at", "REAL"),
            ("ai_feedback_updated_at", "REAL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE messages ADD COLUMN {col} {defn}")
            except:
                pass
        # 性能索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conv_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_ai_feedback ON messages(ai_feedback_updated_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")
        await ensure_message_ingress_dedupe_table(db)
        from app_supervision_ai import ensure_app_supervision_tables
        await ensure_app_supervision_tables(db)
        await _bootstrap_english_corner_schema(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'event',
                created_at REAL NOT NULL,
                source_conv TEXT,
                embedding BLOB
            )
        """)
        # memories 表新增字段（向后兼容迁移）
        for col, defn in [
            ("keywords", "TEXT DEFAULT ''"),
            ("importance", "REAL DEFAULT 0.5"),
            ("source_start_ts", "REAL"),
            ("source_end_ts", "REAL"),
            ("unresolved", "INTEGER DEFAULT 0"),
            ("source_msg_id", "TEXT"),
            ("compression_stage", "INTEGER DEFAULT 0"),
            ("evidence_summary", "TEXT DEFAULT ''"),
            ("evidence_detail_level", "TEXT DEFAULT 'summary'"),
            ("archive_state", "TEXT DEFAULT 'active'"),
            ("archived_at", "REAL"),
            ("period_kind", "TEXT DEFAULT ''"),
            ("period_start_ts", "REAL"),
            ("period_end_ts", "REAL"),
            ("compression_batch_id", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute("UPDATE memories SET compression_stage=1 WHERE type='seeky_compressed' AND COALESCE(compression_stage,0)=0")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_memory_compress_reviews (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL DEFAULT 'both',
                status TEXT NOT NULL DEFAULT 'draft',
                days INTEGER NOT NULL DEFAULT 14,
                cutoff_ts REAL NOT NULL,
                model_main TEXT DEFAULT '',
                model_chatroom TEXT DEFAULT '',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                raw_response TEXT DEFAULT '',
                error TEXT DEFAULT '',
                apply_result TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                applied_at REAL,
                discarded_at REAL
            )
        """)
        try:
            await db.execute("ALTER TABLE daily_memory_compress_reviews ADD COLUMN target TEXT NOT NULL DEFAULT 'both'")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_memory_compress_reviews_created ON daily_memory_compress_reviews(created_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_memory_compress_log (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                old_ids TEXT DEFAULT '[]',
                new_ids TEXT DEFAULT '[]',
                important_ids TEXT DEFAULT '[]',
                message TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_compression_batches (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                level TEXT NOT NULL,
                model_key TEXT NOT NULL,
                status TEXT NOT NULL,
                period_start_ts REAL,
                period_end_ts REAL,
                input_count INTEGER DEFAULT 0,
                output_count INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_compression_batch_inputs (
                batch_id TEXT NOT NULL,
                store TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                PRIMARY KEY (batch_id, store, memory_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_compression_batch_outputs (
                batch_id TEXT NOT NULL,
                store TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                PRIMARY KEY (batch_id, store, memory_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_compression_inputs_memory "
            "ON memory_compression_batch_inputs(store, memory_id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_compression_jobs (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                level TEXT NOT NULL,
                model_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                started_at REAL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_compression_jobs_target "
            "ON memory_compression_jobs(target, level, created_at DESC)"
        )
        # ── 日程/闹铃表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                ended_at REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status, trigger_at)")
        # schedules 表新增字段（向后兼容迁移）
        for col, defn in [
            ("origin", "TEXT DEFAULT 'aion'"),        # 'aion' | 'connor'
            ("origin_room_id", "TEXT DEFAULT ''"),     # 创建时所在的群聊/Connor私聊 room_id（空=Aion私聊）
        ]:
            try:
                await db.execute(f"ALTER TABLE schedules ADD COLUMN {col} {defn}")
            except:
                pass
        # ── 心语表（保留兼容，不再写入新数据） ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS heart_whispers (
                id TEXT PRIMARY KEY,
                conv_id TEXT,
                msg_id TEXT,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_heart_whispers_created ON heart_whispers(created_at DESC)")
        # ── 朋友圈表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moments (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                attachments TEXT DEFAULT '[]',
                source_conv TEXT,
                source_msg_id TEXT,
                expect_reply INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        try:
            await db.execute("ALTER TABLE moments ADD COLUMN attachments TEXT DEFAULT '[]'")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moments_created ON moments(created_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_comments (
                id TEXT PRIMARY KEY,
                moment_id TEXT NOT NULL,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                reply_to_id TEXT,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moment_comments_moment ON moment_comments(moment_id, created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_reactions (
                id TEXT PRIMARY KEY,
                moment_id TEXT NOT NULL,
                author TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(moment_id, author)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_moment_reactions_moment ON moment_reactions(moment_id)")
        # ── 朋友圈已读锚点 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moment_read_anchor (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_read_at REAL NOT NULL DEFAULT 0
            )
        """)
        # ── AI 日记本 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS diary_entries (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                mood TEXT DEFAULT '',
                source_type TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                source_start_ts REAL,
                source_end_ts REAL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_diary_entries_created ON diary_entries(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_diary_entries_author ON diary_entries(author, created_at DESC)")
        # ── 书籍表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT DEFAULT '未知作者',
                cover_path TEXT,
                total_chapters INTEGER DEFAULT 0,
                current_chapter INTEGER DEFAULT 0,
                current_paragraph INTEGER DEFAULT 0,
                import_time REAL NOT NULL
            )
        """)
        # ── 书籍章节表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_chapters (
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
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_index)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_chapters_book ON book_chapters(book_id, chapter_index)")
        # ── 书籍批注表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                segment_index INTEGER NOT NULL,
                annotations TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL,
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE,
                UNIQUE(book_id, chapter_index, segment_index)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_annotations_ch ON book_annotations(book_id, chapter_index)")
        # 迁移：为 book_annotations 添加 annotator 字段（aion/connor）
        try:
            await db.execute("ALTER TABLE book_annotations ADD COLUMN annotator TEXT DEFAULT 'aion'")
        except:
            pass
        # 迁移：去掉旧的 UNIQUE(book_id, chapter_index, segment_index) 约束
        # SQLite 不支持 DROP CONSTRAINT，需要重建表
        try:
            cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='book_annotations'")
            row = await cur.fetchone()
            if row:
                create_sql = row[0] if isinstance(row, (list, tuple)) else row['sql']
                if 'UNIQUE(book_id,chapter_index,segment_index)' in create_sql.replace(' ', ''):
                    await db.execute("PRAGMA foreign_keys = OFF")
                    await db.commit()
                    await db.execute("ALTER TABLE book_annotations RENAME TO _book_annotations_old")
                    await db.execute("""
                        CREATE TABLE book_annotations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            book_id TEXT NOT NULL,
                            chapter_index INTEGER NOT NULL,
                            segment_index INTEGER NOT NULL,
                            annotations TEXT DEFAULT '[]',
                            summary TEXT DEFAULT '',
                            created_at REAL NOT NULL,
                            updated_at REAL,
                            annotator TEXT DEFAULT 'aion',
                            FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
                        )
                    """)
                    await db.execute("""
                        INSERT INTO book_annotations (id, book_id, chapter_index, segment_index,
                            annotations, summary, created_at, updated_at, annotator)
                        SELECT id, book_id, chapter_index, segment_index,
                            annotations, summary, created_at, updated_at, COALESCE(annotator, 'aion')
                        FROM _book_annotations_old
                    """)
                    await db.execute("DROP TABLE _book_annotations_old")
                    await db.execute("CREATE INDEX IF NOT EXISTS idx_book_annotations_ch ON book_annotations(book_id, chapter_index)")
                    await db.commit()
                    await db.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_book_anno_unique ON book_annotations(book_id, chapter_index, segment_index, annotator)")
        # ── 书籍高亮（用户框选提问）表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS book_highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                selected_text TEXT NOT NULL,
                start_p INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_p INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_book_highlights_ch ON book_highlights(book_id, chapter_index)")
        # 迁移：为 book_highlights 添加 annotator 和 connor_answer 字段
        try:
            await db.execute("ALTER TABLE book_highlights ADD COLUMN annotator TEXT DEFAULT 'aion'")
        except:
            pass
        try:
            await db.execute("ALTER TABLE book_highlights ADD COLUMN connor_answer TEXT DEFAULT ''")
        except:
            pass
        # ── 小剧场对话表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS theater_conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                persona_id TEXT,
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_theater_conv_updated ON theater_conversations(updated_at DESC)")
        # ── 小剧场消息表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS theater_messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT '[]',
                reasoning_content TEXT DEFAULT '',
                FOREIGN KEY (conv_id) REFERENCES theater_conversations(id) ON DELETE CASCADE
            )
        """)
        try:
            await db.execute("ALTER TABLE theater_messages ADD COLUMN reasoning_content TEXT DEFAULT ''")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_theater_msg_conv ON theater_messages(conv_id, created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS date_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                prompt TEXT DEFAULT '',
                outline TEXT DEFAULT '',
                opening TEXT DEFAULT '',
                partner_name TEXT DEFAULT '',
                persona TEXT DEFAULT '',
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                ending_trigger TEXT DEFAULT '',
                outline_usage TEXT DEFAULT '{}',
                end_usage TEXT DEFAULT '{}',
                current_background TEXT DEFAULT '背景-客厅',
                current_state TEXT DEFAULT '平静',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                ended_at REAL,
                synced_at REAL,
                synced_target TEXT DEFAULT ''
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_date_sessions_updated ON date_sessions(updated_at DESC)")
        for col_name, ddl in (
            ("prompt", "ALTER TABLE date_sessions ADD COLUMN prompt TEXT DEFAULT ''"),
            ("outline", "ALTER TABLE date_sessions ADD COLUMN outline TEXT DEFAULT ''"),
            ("opening", "ALTER TABLE date_sessions ADD COLUMN opening TEXT DEFAULT ''"),
            ("partner_name", "ALTER TABLE date_sessions ADD COLUMN partner_name TEXT DEFAULT ''"),
            ("outline_usage", "ALTER TABLE date_sessions ADD COLUMN outline_usage TEXT DEFAULT '{}'"),
            ("end_usage", "ALTER TABLE date_sessions ADD COLUMN end_usage TEXT DEFAULT '{}'"),
        ):
            try:
                await db.execute(ddl)
            except:
                pass
        await migrate_schedule_history(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS date_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT '[]',
                FOREIGN KEY (session_id) REFERENCES date_sessions(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_date_messages_session ON date_messages(session_id, created_at)")
        # ── 礼物表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                id TEXT PRIMARY KEY,
                image_path TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                received_at REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gifts_status ON gifts(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gifts_created ON gifts(created_at DESC)")
        # gifts 表新增字段（向后兼容迁移）
        try:
            await db.execute("ALTER TABLE gifts ADD COLUMN sender TEXT DEFAULT 'aion'")
        except:
            pass
        # ── 基金持仓表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fund_holdings (
                id TEXT PRIMARY KEY,
                fund_code TEXT NOT NULL,
                fund_name TEXT DEFAULT '',
                shares REAL DEFAULT 0,
                avg_cost REAL DEFAULT 0,
                total_cost REAL DEFAULT 0,
                warn_down REAL DEFAULT -3.0,
                warn_up REAL DEFAULT 15.0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fund_holdings_code ON fund_holdings(fund_code)")
        # ── 娱乐室日志表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS playground_logs (
                id TEXT PRIMARY KEY,
                server TEXT NOT NULL,
                instruction TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                summary TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        try:
            await db.execute("ALTER TABLE playground_logs ADD COLUMN summary TEXT DEFAULT ''")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_playground_logs_created ON playground_logs(created_at DESC)")
        # ── 聊天室房间表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_rooms (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'group',
                aion_persona TEXT DEFAULT '',
                connor_persona TEXT DEFAULT '',
                context_minutes INTEGER DEFAULT 30,
                ai_chat_rounds INTEGER DEFAULT 3,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_rooms_updated ON chatroom_rooms(updated_at DESC)")
        # ── 聊天室消息表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_messages (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                attachments TEXT DEFAULT '[]',
                created_at REAL NOT NULL,
                FOREIGN KEY (room_id) REFERENCES chatroom_rooms(id) ON DELETE CASCADE
            )
        """)
        for col, defn in [
            ("reasoning_content", "TEXT DEFAULT ''"),
            ("ai_feedback_rating", "TEXT DEFAULT ''"),
            ("ai_feedback_reason", "TEXT DEFAULT ''"),
            ("ai_feedback_created_at", "REAL"),
            ("ai_feedback_updated_at", "REAL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE chatroom_messages ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_msg_room ON chatroom_messages(room_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_msg_created ON chatroom_messages(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_ai_feedback ON chatroom_messages(ai_feedback_updated_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS persona_evolution_runs (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL DEFAULT 'main_ai',
                trigger TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'draft',
                model TEXT DEFAULT '',
                window_start_ts REAL NOT NULL,
                window_end_ts REAL NOT NULL,
                window_label TEXT DEFAULT '',
                feedback_count INTEGER NOT NULL DEFAULT 0,
                summary TEXT DEFAULT '',
                patch_json TEXT DEFAULT '{}',
                user_message TEXT DEFAULT '',
                raw_response TEXT DEFAULT '',
                error TEXT DEFAULT '',
                auto_applied INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                applied_at REAL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_persona_evolution_runs_actor_created ON persona_evolution_runs(actor, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_persona_evolution_runs_window ON persona_evolution_runs(actor, window_start_ts, window_end_ts)")
        for col, defn in [
            ("before_json", "TEXT DEFAULT '{}'"),
            ("after_json", "TEXT DEFAULT '{}'"),
            ("diff_json", "TEXT DEFAULT '[]'"),
            ("feedback_fingerprint", "TEXT DEFAULT ''"),
            ("daily_memory_json", "TEXT DEFAULT '[]'"),
        ]:
            try:
                await db.execute(f"ALTER TABLE persona_evolution_runs ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS persona_evolution_items (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                message_id TEXT NOT NULL,
                conv_id TEXT DEFAULT '',
                room_id TEXT DEFAULT '',
                room_title TEXT DEFAULT '',
                speaker TEXT DEFAULT '',
                rating TEXT NOT NULL,
                reason TEXT DEFAULT '',
                message_content TEXT DEFAULT '',
                context_json TEXT DEFAULT '[]',
                message_created_at REAL,
                feedback_updated_at REAL,
                created_at REAL NOT NULL,
                FOREIGN KEY (run_id) REFERENCES persona_evolution_runs(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_persona_evolution_items_run ON persona_evolution_items(run_id)")
        # ── 聊天室记忆表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_memories (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                scope TEXT DEFAULT 'group',
                content TEXT NOT NULL,
                keywords TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                embedding BLOB,
                source_start_ts REAL,
                source_end_ts REAL,
                created_at REAL NOT NULL,
                unresolved INTEGER DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_mem_room ON chatroom_memories(room_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chatroom_mem_scope ON chatroom_memories(scope)")
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN source_msg_id TEXT")
        except:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN memory_kind TEXT DEFAULT 'long_term'")
        except:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN compression_stage INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN evidence_summary TEXT DEFAULT ''")
        except:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN evidence_detail_level TEXT DEFAULT 'summary'")
        except:
            pass
        for col, defn in [
            ("archive_state", "TEXT DEFAULT 'active'"),
            ("archived_at", "REAL"),
            ("period_kind", "TEXT DEFAULT ''"),
            ("period_start_ts", "REAL"),
            ("period_end_ts", "REAL"),
            ("compression_batch_id", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE chatroom_memories ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute(
            "UPDATE chatroom_memories SET memory_kind='daily' "
            "WHERE (memory_kind IS NULL OR memory_kind='' OR memory_kind='long_term') "
            "AND source_start_ts IS NOT NULL AND source_end_ts IS NOT NULL "
            "AND (source_msg_id IS NULL OR TRIM(source_msg_id)='')"
        )
        # ── 聊天室总结锚点表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS idle_events (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT DEFAULT '',
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                result_type TEXT DEFAULT '',
                result_id TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_idle_events_created ON idle_events(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_idle_events_actor ON idle_events(actor, created_at DESC)")
        # 鈹€鈹€ 许愿池 鈹€鈹€
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishes (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                author_name TEXT DEFAULT '',
                content TEXT NOT NULL,
                category TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                visibility TEXT DEFAULT 'shared',
                origin TEXT DEFAULT 'manual',
                source_type TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                source_start_ts REAL,
                source_end_ts REAL,
                pulled_count INTEGER NOT NULL DEFAULT 0,
                last_pulled_at REAL,
                fulfilled_at REAL,
                released_at REAL,
                last_mentioned_at REAL,
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        for col, defn in [
            ("author_name", "TEXT DEFAULT ''"),
            ("category", "TEXT DEFAULT ''"),
            ("visibility", "TEXT DEFAULT 'shared'"),
            ("origin", "TEXT DEFAULT 'manual'"),
            ("source_type", "TEXT DEFAULT ''"),
            ("source_ref", "TEXT DEFAULT ''"),
            ("source_start_ts", "REAL"),
            ("source_end_ts", "REAL"),
            ("pulled_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_pulled_at", "REAL"),
            ("fulfilled_at", "REAL"),
            ("released_at", "REAL"),
            ("last_mentioned_at", "REAL"),
            ("metadata", "TEXT DEFAULT '{}'"),
            ("updated_at", "REAL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE wishes ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wishes_status ON wishes(status, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wishes_author_status ON wishes(author, status, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wishes_pulled ON wishes(last_pulled_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_digest_anchors (
                room_id TEXT PRIMARY KEY,
                anchor_ts REAL NOT NULL DEFAULT 0
            )
        """)
        # ── 活动轨迹表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS life_trajectory (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_life_trajectory_created ON life_trajectory(created_at DESC)")
        # ── 记账与生理期表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookkeeping (
                id TEXT PRIMARY KEY,
                record_type TEXT NOT NULL,
                amount REAL DEFAULT 0,
                description TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookkeeping_created ON bookkeeping(created_at DESC)")
        # ── 健康数据表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_ring_latest (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                device_name TEXT DEFAULT '',
                heart_rate INTEGER,
                systolic_bp INTEGER,
                diastolic_bp INTEGER,
                spo2 INTEGER,
                hrv REAL,
                measured_at REAL,
                sleep_start_at REAL,
                sleep_end_at REAL,
                sleep_total_min INTEGER,
                sleep_deep_min INTEGER,
                sleep_light_min INTEGER,
                sleep_rem_min INTEGER,
                sleep_wake_min INTEGER,
                sleep_wake_count INTEGER,
                raw_json TEXT DEFAULT '',
                synced_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_ring_heart_rates (
                id TEXT PRIMARY KEY,
                device_name TEXT DEFAULT '',
                heart_rate INTEGER NOT NULL,
                measured_at REAL NOT NULL,
                source TEXT DEFAULT '',
                raw_json TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_ring_heart_rates_measured ON health_ring_heart_rates(measured_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_miband_activity (
                source TEXT NOT NULL,
                measured_at REAL NOT NULL,
                device_name TEXT DEFAULT '',
                raw_kind INTEGER NOT NULL DEFAULT 0,
                intensity INTEGER NOT NULL DEFAULT 0,
                steps INTEGER NOT NULL DEFAULT 0,
                heart_rate INTEGER NOT NULL DEFAULT 0,
                unknown_value INTEGER NOT NULL DEFAULT 0,
                sleep_value INTEGER NOT NULL DEFAULT 0,
                deep_sleep_value INTEGER NOT NULL DEFAULT 0,
                rem_sleep_value INTEGER NOT NULL DEFAULT 0,
                sleep_stage TEXT DEFAULT '',
                synced_at REAL NOT NULL,
                PRIMARY KEY (source, measured_at)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_miband_activity_measured ON health_miband_activity(measured_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_miband_commands (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL CHECK (pattern IN ('single','call')),
                source_type TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_msg_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                acknowledged_at REAL
            )
        """)
        for col, definition in [
            ("note", "TEXT NOT NULL DEFAULT ''"),
            ("sender_name", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE health_miband_commands ADD COLUMN {col} {definition}"
                )
            except Exception:
                pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_health_miband_commands_pending "
            "ON health_miband_commands(acknowledged_at, expires_at, created_at)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_heart_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                sleep_low_max INTEGER NOT NULL DEFAULT 65,
                normal_min INTEGER NOT NULL DEFAULT 70,
                normal_max INTEGER NOT NULL DEFAULT 95,
                elevated_min INTEGER NOT NULL DEFAULT 96,
                exercise_min INTEGER NOT NULL DEFAULT 100,
                attention_low INTEGER NOT NULL DEFAULT 45,
                attention_high INTEGER NOT NULL DEFAULT 135,
                large_delta INTEGER NOT NULL DEFAULT 25,
                night_start_hour INTEGER NOT NULL DEFAULT 0,
                night_end_hour INTEGER NOT NULL DEFAULT 6,
                stale_minutes INTEGER NOT NULL DEFAULT 30,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_heart_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_category TEXT DEFAULT '',
                last_heart_rate INTEGER,
                last_measured_at REAL,
                sleep_candidate_since REAL,
                high_candidate_since REAL,
                last_event_type TEXT DEFAULT '',
                last_event_at REAL,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_heart_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                heart_rate INTEGER NOT NULL,
                previous_heart_rate INTEGER,
                delta INTEGER,
                category TEXT DEFAULT '',
                previous_category TEXT DEFAULT '',
                measured_at REAL NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_heart_events_created ON health_heart_events(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_heart_events_measured ON health_heart_events(measured_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_weight_entries (
                date TEXT PRIMARY KEY,
                weight_kg REAL NOT NULL,
                note TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_weight_date ON health_weight_entries(date DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_period_entries (
                id TEXT PRIMARY KEY,
                start_date TEXT NOT NULL,
                end_date TEXT DEFAULT '',
                flow TEXT DEFAULT '',
                symptoms TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_health_period_start ON health_period_entries(start_date DESC)")
        # ── 跨端增量同步事件 ──
        # WebSocket 负责低延迟；此表负责设备休眠、断网或重连后的可靠补齐。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT '',
                entity_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sync_events_created ON sync_events(created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.commit()


def get_db():
    return aiosqlite.connect(DB_PATH)
