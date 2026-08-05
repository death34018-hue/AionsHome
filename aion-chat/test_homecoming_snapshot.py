import json
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class HomecomingSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "snapshot.db"
        self.now = 1_800_000_000.0
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, title TEXT, model TEXT,
                    created_at REAL, updated_at REAL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT, role TEXT,
                    content TEXT, attachments TEXT, created_at REAL
                );
                CREATE TABLE chatroom_rooms (
                    id TEXT PRIMARY KEY, title TEXT, type TEXT,
                    aion_persona TEXT, connor_persona TEXT,
                    context_minutes INTEGER, ai_chat_rounds INTEGER,
                    created_at REAL, updated_at REAL
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY, room_id TEXT, sender TEXT,
                    content TEXT, attachments TEXT, created_at REAL
                );
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT, type TEXT,
                    created_at REAL, source_conv TEXT, embedding BLOB,
                    keywords TEXT, importance REAL, source_start_ts REAL,
                    source_end_ts REAL, unresolved INTEGER,
                    source_msg_id TEXT, evidence_summary TEXT,
                    evidence_detail_level TEXT, archive_state TEXT
                );
                CREATE TABLE chatroom_memories (
                    id TEXT PRIMARY KEY, room_id TEXT, scope TEXT,
                    content TEXT, keywords TEXT, importance REAL,
                    embedding BLOB, source_start_ts REAL,
                    source_end_ts REAL, created_at REAL,
                    unresolved INTEGER, source_msg_id TEXT,
                    evidence_summary TEXT, evidence_detail_level TEXT,
                    archive_state TEXT
                );
                CREATE TABLE schedules (
                    id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT,
                    content TEXT, created_at REAL, status TEXT,
                    ended_at REAL, origin TEXT, origin_room_id TEXT
                );
                """
            )
            await db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?)",
                ("conv-main", "Main", "cloud-model", self.now - 1000, self.now - 10),
            )
            await db.executemany(
                "INSERT INTO chatroom_rooms VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("room-second", "Second", "connor_1v1", "", "", 30, 1,
                     self.now - 1000, self.now - 10),
                    ("room-group", "Group", "group", "", "", 30, 3,
                     self.now - 1000, self.now - 10),
                    ("room-excluded", "Game", "doudizhu", "", "", 30, 1,
                     self.now - 1000, self.now - 10),
                ],
            )
            recent_rows = [
                (
                    f"msg-{index:04d}",
                    "conv-main",
                    "user" if index % 2 == 0 else "assistant",
                    "看这张图" if index == 3001 else f"message {index}",
                    json.dumps([{
                        "url": "/uploads/private.jpg",
                        "type": "image",
                        "name": "private.jpg",
                    }]) if index == 3001 else "[]",
                    self.now - (3002 - index),
                )
                for index in range(3002)
            ]
            recent_rows.append((
                "msg-too-old", "conv-main", "user", "old", "[]",
                self.now - 91 * 86400,
            ))
            await db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)", recent_rows
            )
            await db.executemany(
                "INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)",
                [
                    ("second-1", "room-second", "user", "hello", "[]", self.now - 8),
                    ("group-1", "room-group", "connor", "group hello", "[]", self.now - 7),
                    ("game-1", "room-excluded", "user", "not portable", "[]", self.now - 6),
                ],
            )
            await db.executemany(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("main-active", "active memory", "event", self.now - 5, "conv-main",
                     b"\x00\x01", "active", 0.8, self.now - 8, self.now - 7, 0,
                     "msg-3000", "evidence", "summary", "active"),
                    ("main-archived", "archived memory", "event", self.now - 4, "conv-main",
                     None, "", 0.3, None, None, 0, None, "", "summary", "archived"),
                ],
            )
            await db.execute(
                "INSERT INTO chatroom_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("second-active", "room-second", "connor", "second memory", "second",
                 0.9, None, self.now - 8, self.now - 7, self.now - 3, 0,
                 "second-1", "evidence", "summary", "active"),
            )
            await db.executemany(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("schedule-active", "reminder", "2030-01-01T10:00:00",
                     "remember", self.now - 2, "active", None, "aion", ""),
                    ("schedule-done", "reminder", "2020-01-01T10:00:00",
                     "done", self.now - 2, "completed", self.now - 1, "aion", ""),
                ],
            )
            await db.commit()

        self.worldbook = {
            "user_name": "Configured User",
            "user_persona": "User persona",
            "ai_name": "Configured AI",
            "ai_persona": "AI persona",
            "system_prompt": "System rules",
        }
        self.chatroom_config = {
            "connor_name": "Configured Second",
            "connor_persona": "Second persona",
            "reply_order": "random",
            "tts_enabled": True,
        }

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def build(self, db):
        from homecoming.snapshot import build_snapshot_sections

        return await build_snapshot_sections(
            db,
            now=self.now,
            supervision_snapshot={"groups": [{"id": "study"}]},
            worldbook=self.worldbook,
            chatroom_config=self.chatroom_config,
            settings={"temperature": 0.7, "idle_autonomy_enabled": True},
            location_config={"enabled": True},
            location_status={"state": "home"},
            camera_config={"monitor_enabled": True},
            digest_anchor={"last_ts": self.now - 20},
        )

    async def test_selects_only_supported_timelines_and_limits_main_history(self):
        async with aiosqlite.connect(self.db_path) as db:
            sections = await self.build(db)

        self.assertEqual(
            {"main_private", "companion_private", "group"},
            set(sections["timelines"]),
        )
        main = sections["timelines"]["main_private"]["messages"]
        self.assertEqual(3000, len(main))
        self.assertEqual("msg-0002", main[0]["id"])
        self.assertEqual("msg-3001", main[-1]["id"])
        serialized = json.dumps(sections, ensure_ascii=False)
        self.assertNotIn("msg-too-old", serialized)
        self.assertNotIn("game-1", serialized)

    async def test_strips_media_locations_but_keeps_text_and_attachment_kind(self):
        async with aiosqlite.connect(self.db_path) as db:
            sections = await self.build(db)

        message = sections["timelines"]["main_private"]["messages"][-1]
        self.assertEqual("看这张图", message["content"])
        self.assertEqual([{"kind": "image"}], message["attachments"])
        serialized = json.dumps(message, ensure_ascii=False)
        self.assertNotIn("/uploads/", serialized)
        self.assertNotIn("private.jpg", serialized)

    async def test_exports_active_memories_pending_schedules_and_configured_names(self):
        async with aiosqlite.connect(self.db_path) as db:
            sections = await self.build(db)

        self.assertEqual(
            ["main-active"],
            [row["id"] for row in sections["memories"]["main"]],
        )
        self.assertEqual(
            ["second-active"],
            [row["id"] for row in sections["memories"]["second"]],
        )
        self.assertEqual(
            ["schedule-active"],
            [row["id"] for row in sections["schedules"]],
        )
        self.assertEqual("Configured User", sections["identity"]["user"]["name"])
        self.assertEqual(
            "Configured AI", sections["identity"]["companions"]["main"]["name"]
        )
        self.assertEqual(
            "Configured Second", sections["identity"]["companions"]["second"]["name"]
        )
        self.assertNotIn("idle_autonomy_enabled", sections["runtime_state"]["settings"])

    async def test_builder_never_writes_main_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            before = db.total_changes
            await self.build(db)
            self.assertEqual(before, db.total_changes)


if __name__ == "__main__":
    unittest.main()
