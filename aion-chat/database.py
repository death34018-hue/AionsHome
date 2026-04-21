"""
数据库初始化与连接
"""

import aiosqlite
from config import DB_PATH


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
        # 性能索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conv_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")
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
        ]:
            try:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {defn}")
            except:
                pass
        # ── 日程/闹铃表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status, trigger_at)")
        # ── 心语表 ──
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
                FOREIGN KEY (conv_id) REFERENCES theater_conversations(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_theater_msg_conv ON theater_messages(conv_id, created_at)")
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
        await db.commit()


def get_db():
    return aiosqlite.connect(DB_PATH)
