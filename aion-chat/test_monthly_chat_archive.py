import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from monthly_chat_archive import archive_chat_windows


JUNE = 1780329600.0  # 2026-06-01 08:00 Asia/Shanghai
JULY = 1783008000.0  # 2026-07-02 08:00 Asia/Shanghai


class MonthlyChatArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "chat.db"
        self.backup_dir = self.root / "backups"
        self._create_fixture()

    def tearDown(self):
        self.doCleanups()
        self.temp_dir.cleanup()

    def _create_fixture(self):
        db = sqlite3.connect(self.db_path)
        db.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at REAL NOT NULL,
                attachments TEXT DEFAULT '', starred INTEGER DEFAULT 0,
                ai_feedback_rating TEXT DEFAULT '', ai_feedback_reason TEXT DEFAULT '',
                reasoning_content TEXT DEFAULT '',
                FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE TABLE chatroom_rooms (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL,
                aion_persona TEXT DEFAULT '', connor_persona TEXT DEFAULT '',
                context_minutes INTEGER DEFAULT 30, ai_chat_rounds INTEGER DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE chatroom_messages (
                id TEXT PRIMARY KEY, room_id TEXT NOT NULL, sender TEXT NOT NULL,
                content TEXT NOT NULL, attachments TEXT DEFAULT '[]',
                created_at REAL NOT NULL, reasoning_content TEXT DEFAULT '',
                ai_feedback_rating TEXT DEFAULT '', ai_feedback_reason TEXT DEFAULT '',
                FOREIGN KEY (room_id) REFERENCES chatroom_rooms(id) ON DELETE CASCADE
            );
            CREATE TABLE heart_whispers (id TEXT PRIMARY KEY, conv_id TEXT, msg_id TEXT, content TEXT, created_at REAL);
            CREATE TABLE memory_sources (id TEXT PRIMARY KEY, record_id TEXT, source_store TEXT, message_id TEXT, conversation_id TEXT, room_id TEXT, source_created_at REAL, excerpt TEXT);
            CREATE TABLE persona_evolution_items (id TEXT PRIMARY KEY, run_id TEXT, source TEXT, message_id TEXT, conv_id TEXT, room_id TEXT, room_title TEXT, speaker TEXT, rating TEXT, created_at REAL);
            CREATE TABLE health_miband_commands (id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, source_msg_id TEXT, created_at REAL);
            CREATE TABLE message_ingress_dedupe (dedupe_key TEXT PRIMARY KEY, target_type TEXT, target_id TEXT, message_id TEXT, created_at REAL);
            CREATE TABLE schedules (id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT, created_at REAL, status TEXT, origin TEXT, origin_room_id TEXT, ended_at REAL);
            CREATE TABLE chatroom_memories (id TEXT PRIMARY KEY, room_id TEXT, scope TEXT, content TEXT, source_start_ts REAL, source_end_ts REAL, created_at REAL, source_msg_id TEXT);
            CREATE TABLE chatroom_digest_anchors (room_id TEXT PRIMARY KEY, anchor_ts REAL NOT NULL);
            CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL, source_conv TEXT, source_start_ts REAL, source_end_ts REAL, source_msg_id TEXT);
            CREATE TABLE moments (id TEXT PRIMARY KEY, author TEXT, content TEXT, source_conv TEXT, source_msg_id TEXT, created_at REAL);
            CREATE TABLE unrelated_parent (id TEXT PRIMARY KEY);
            CREATE TABLE unrelated_child (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                FOREIGN KEY (parent_id) REFERENCES unrelated_parent(id)
            );
            """
        )
        db.executemany(
            "INSERT INTO conversations VALUES (?,?,?,?,?)",
            [
                ("conv_old", "旧Aion", "model-a", JUNE, JUNE + 10),
                ("conv_active", "Aion", "model-a", JULY, JULY + 100),
            ],
        )
        db.executemany(
            "INSERT INTO messages (id,conv_id,role,content,created_at,attachments,starred,ai_feedback_rating,ai_feedback_reason,reasoning_content) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("m_june", "conv_old", "user", "六月", JUNE, '[{"x":1}]', 1, "up", "好", "想法"),
                ("m_july", "conv_old", "assistant", "七月", JULY, "[]", 0, "", "", ""),
            ],
        )
        rooms = []
        for kind, room_type, suffix in (("Connor", "connor_1v1", "c"), ("群聊", "group", "g")):
            rooms.extend(
                [
                    (f"cr_old_{suffix}", f"旧{kind}", room_type, "a", "c", 30, 2, JUNE, JUNE + 10),
                    (f"cr_active_{suffix}", kind, room_type, "a", "c", 30, 2, JULY, JULY + 100),
                ]
            )
        db.executemany("INSERT INTO chatroom_rooms VALUES (?,?,?,?,?,?,?,?,?)", rooms)
        db.executemany(
            "INSERT INTO chatroom_messages (id,room_id,sender,content,attachments,created_at,reasoning_content,ai_feedback_rating,ai_feedback_reason) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("cm_c_june", "cr_old_c", "connor", "C六月", "[]", JUNE + 1, "c想法", "up", "好"),
                ("cm_c_july", "cr_old_c", "user", "C七月", "[]", JULY + 1, "", "", ""),
                ("cm_g_june", "cr_old_g", "aion", "G六月", "[]", JUNE + 2, "g想法", "", ""),
                ("cm_g_july", "cr_old_g", "user", "G七月", "[]", JULY + 2, "", "", ""),
            ],
        )
        db.execute("INSERT INTO heart_whispers VALUES ('w1','conv_old','m_june','悄悄话',?)", (JUNE,))
        db.execute("INSERT INTO heart_whispers VALUES ('w2','conv_old','missing-message','旧密语',?)", (JUNE + 3,))
        db.executemany(
            "INSERT INTO memory_sources VALUES (?,?,?,?,?,?,?,?)",
            [
                ("ms1", "r1", "messages", "m_june", "conv_old", None, JUNE, "六月"),
                ("ms2", "r2", "chatroom_messages", "cm_g_july", None, "cr_old_g", JULY, "七月"),
                ("ms3", "r3", "private", "missing-private", "conv_old", None, None, "旧私聊来源"),
                ("ms4", "r4", "chatroom", "missing-room", None, "cr_old_g", JULY, "旧群聊来源"),
                ("ms5", "r5", "chatroom", "missing-orphan", None, "cr_orphan", None, "旧孤立来源"),
            ],
        )
        db.execute("INSERT INTO persona_evolution_items VALUES ('p1','run','private','m_june','conv_old','','Aion','user','up',?)", (JUNE,))
        db.execute("INSERT INTO health_miband_commands VALUES ('h1','chatroom','cr_old_g','cm_g_july',?)", (JULY,))
        db.execute("INSERT INTO message_ingress_dedupe VALUES ('d1','chatroom','cr_old_g','cm_g_july',?)", (JULY,))
        db.execute("INSERT INTO schedules VALUES ('s1','monitor','2026-07-03','提醒',?,'active','connor','cr_old_c',NULL)", (JULY,))
        db.execute("INSERT INTO chatroom_memories VALUES ('crm1','cr_old_g','group','群记忆',?,?,?,'cm_g_july')", (JULY, JULY, JULY))
        db.execute("INSERT INTO chatroom_memories VALUES ('crm2','cr_old_c','connor','C记忆',?,?,?,NULL)", (JUNE, JUNE, JUNE))
        db.execute("INSERT INTO chatroom_memories VALUES ('crm3','cr_orphan','connor','孤立C记忆',?,?,?,NULL)", (JUNE, JUNE, JUNE))
        db.execute("INSERT INTO chatroom_digest_anchors VALUES ('cr_old_g',?)", (JULY + 2,))
        db.execute("INSERT INTO memories VALUES ('mem1','私聊记忆','event',?,'conv_old',?,?, 'm_june')", (JUNE, JUNE, JUNE))
        db.execute("INSERT INTO moments VALUES ('mt1','aion','动态','conv_old','m_june',?)", (JUNE,))
        db.commit()
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO unrelated_child VALUES ('existing-warning','missing-parent')")
        db.commit()
        db.close()

    def test_archives_each_kind_by_local_month_without_losing_payloads(self):
        report = archive_chat_windows(self.db_path, self.backup_dir)

        self.assertTrue(report.backup_path.exists())
        self.assertEqual(report.before_counts, {"aion": 2, "connor": 2, "group": 2})
        self.assertEqual(report.after_counts, report.before_counts)

        db = sqlite3.connect(self.db_path)
        self.addCleanup(db.close)
        self.assertEqual(
            db.execute("SELECT title FROM conversations ORDER BY updated_at DESC").fetchall(),
            [("Aion 26-7",), ("Aion 26-6",)],
        )
        self.assertEqual(
            db.execute("SELECT title FROM chatroom_rooms WHERE type='connor_1v1' ORDER BY updated_at DESC").fetchall(),
            [("Connor 26-7",), ("Connor 26-6",)],
        )
        self.assertEqual(
            db.execute("SELECT title FROM chatroom_rooms WHERE type='group' ORDER BY updated_at DESC").fetchall(),
            [("群聊26-7",), ("群聊26-6",)],
        )
        self.assertEqual(db.execute("SELECT conv_id FROM messages WHERE id='m_july'").fetchone()[0], "conv_active")
        self.assertEqual(db.execute("SELECT room_id FROM chatroom_messages WHERE id='cm_c_july'").fetchone()[0], "cr_active_c")
        self.assertEqual(db.execute("SELECT room_id FROM chatroom_messages WHERE id='cm_g_july'").fetchone()[0], "cr_active_g")
        self.assertEqual(
            db.execute("SELECT content,attachments,starred,ai_feedback_rating,ai_feedback_reason,reasoning_content FROM messages WHERE id='m_june'").fetchone(),
            ("六月", '[{"x":1}]', 1, "up", "好", "想法"),
        )
        june_conv = db.execute("SELECT conv_id FROM messages WHERE id='m_june'").fetchone()[0]
        july_group = db.execute("SELECT room_id FROM chatroom_messages WHERE id='cm_g_july'").fetchone()[0]
        self.assertEqual(db.execute("SELECT conv_id FROM heart_whispers WHERE id='w1'").fetchone()[0], june_conv)
        self.assertEqual(db.execute("SELECT conv_id FROM heart_whispers WHERE id='w2'").fetchone()[0], june_conv)
        self.assertEqual(db.execute("SELECT conversation_id FROM memory_sources WHERE id='ms1'").fetchone()[0], june_conv)
        self.assertEqual(db.execute("SELECT conversation_id FROM memory_sources WHERE id='ms3'").fetchone()[0], june_conv)
        self.assertEqual(db.execute("SELECT room_id FROM memory_sources WHERE id='ms2'").fetchone()[0], july_group)
        self.assertEqual(db.execute("SELECT room_id FROM memory_sources WHERE id='ms4'").fetchone()[0], july_group)
        self.assertEqual(db.execute("SELECT source_id FROM health_miband_commands WHERE id='h1'").fetchone()[0], july_group)
        self.assertEqual(db.execute("SELECT target_id FROM message_ingress_dedupe WHERE dedupe_key='d1'").fetchone()[0], july_group)
        self.assertEqual(db.execute("SELECT origin_room_id FROM schedules WHERE id='s1'").fetchone()[0], "cr_active_c")
        self.assertEqual(db.execute("SELECT room_id FROM chatroom_memories WHERE id='crm1'").fetchone()[0], july_group)
        june_connor = db.execute("SELECT room_id FROM chatroom_messages WHERE id='cm_c_june'").fetchone()[0]
        self.assertEqual(db.execute("SELECT room_id FROM chatroom_memories WHERE id='crm2'").fetchone()[0], june_connor)
        self.assertEqual(db.execute("SELECT room_id FROM chatroom_memories WHERE id='crm3'").fetchone()[0], june_connor)
        self.assertEqual(db.execute("SELECT room_id FROM memory_sources WHERE id='ms5'").fetchone()[0], june_connor)
        self.assertEqual(db.execute("SELECT source_conv FROM memories WHERE id='mem1'").fetchone()[0], june_conv)
        self.assertEqual(db.execute("SELECT source_conv FROM moments WHERE id='mt1'").fetchone()[0], june_conv)
        self.assertEqual(
            [row[0] for row in db.execute("PRAGMA foreign_key_check").fetchall()],
            ["unrelated_child"],
        )
        db.close()

    def test_backup_keeps_the_original_window_inventory(self):
        report = archive_chat_windows(self.db_path, self.backup_dir)

        with closing(sqlite3.connect(f"file:{report.backup_path.as_posix()}?mode=ro", uri=True)) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM chatroom_rooms").fetchone()[0], 4)
            self.assertEqual(backup.execute("SELECT title FROM conversations ORDER BY id").fetchall(), [("Aion",), ("旧Aion",)])


if __name__ == "__main__":
    unittest.main()
