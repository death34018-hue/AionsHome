import json
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def operation(
    op_id,
    entity_type,
    action,
    payload,
    *,
    entity_id=None,
    sequence=1,
    base_revision="",
):
    return {
        "op_id": op_id,
        "device_seq": sequence,
        "entity_type": entity_type,
        "entity_id": entity_id or op_id,
        "action": action,
        "base_revision": base_revision,
        "payload": payload,
        "created_at": 1000 + sequence,
    }


def checkpoint_operation(sequence, source_ids, *, owner="main"):
    from homecoming.contracts import canonical_json_bytes, sha256_hex

    core = {
        "checkpoint_id": f"checkpoint-{sequence}",
        "epoch_id": "epoch-one",
        "owner_id": owner,
        "previous_message_id": "",
        "last_message_id": source_ids[-1],
        "source_message_ids": source_ids,
        "memory_ids": [],
    }
    payload = dict(core, payload_sha256=sha256_hex(canonical_json_bytes(core)))
    return operation(
        f"checkpoint-op-{sequence}",
        "summary_checkpoint",
        "create",
        payload,
        entity_id=core["checkpoint_id"],
        sequence=sequence,
    )


class HomecomingReturnPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "planner.db"
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
                    keywords TEXT, importance REAL, source_msg_id TEXT,
                    archive_state TEXT
                );
                CREATE TABLE chatroom_memories (
                    id TEXT PRIMARY KEY, room_id TEXT, scope TEXT,
                    content TEXT, keywords TEXT, importance REAL,
                    embedding BLOB, created_at REAL, source_msg_id TEXT,
                    archive_state TEXT
                );
                CREATE TABLE schedules (
                    id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT,
                    content TEXT, created_at REAL, status TEXT,
                    ended_at REAL, origin TEXT, origin_room_id TEXT
                );
                CREATE TABLE homecoming_snapshot_exports (
                    device_id TEXT, snapshot_id TEXT, etag TEXT,
                    manifest_json TEXT, timeline_mapping_json TEXT,
                    created_at REAL,
                    PRIMARY KEY (device_id, snapshot_id)
                );
                """
            )
            await db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?)",
                ("conv-main", "Main", "model", 1, 10),
            )
            await db.executemany(
                "INSERT INTO chatroom_rooms VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("room-second", "Second", "connor_1v1", "", "", 30, 1, 1, 10),
                    ("room-group", "Group", "group", "", "", 30, 3, 1, 10),
                ],
            )
            await db.execute(
                "INSERT INTO homecoming_snapshot_exports VALUES (?,?,?,?,?,?)",
                (
                    "android:test-device",
                    "snapshot-one",
                    '"snapshot-one"',
                    "{}",
                    json.dumps(
                        {
                            "main_private": "conv-main",
                            "companion_private": "room-second",
                            "group": "room-group",
                        }
                    ),
                    10,
                ),
            )
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def package(self, *operations):
        return {
            "package_id": "return-test",
            "device_id": "android:test-device",
            "epoch_id": "epoch-one",
            "base_snapshot_id": "snapshot-one",
            "operations": list(operations),
        }

    async def plan(self, *operations, now=2000):
        from homecoming.return_planner import build_import_plan

        async with aiosqlite.connect(self.db_path) as db:
            return await build_import_plan(db, self.package(*operations), now=now)

    async def test_message_collision_keeps_server_and_never_updates_it(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                ("same", "conv-main", "user", "server", "[]", 1),
            )
            await db.commit()
        plan = await self.plan(
            operation(
                "same",
                "message",
                "create",
                {
                    "id": "same",
                    "timeline_id": "main_private",
                    "role": "user",
                    "sender_id": "user",
                    "text": "phone",
                    "attachment_kind": "",
                    "attachment_transcript": "",
                    "created_at": 2,
                },
            )
        )
        self.assertEqual("quarantine", plan.rows[0].decision)
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT content FROM messages WHERE id='same'")
            ).fetchone()
        self.assertEqual("server", row[0])

    async def test_same_message_is_duplicate_and_new_mapped_message_applies(self):
        payload = {
            "id": "same",
            "timeline_id": "main_private",
            "role": "user",
            "sender_id": "user",
            "text": "same text",
            "attachment_kind": "",
            "attachment_transcript": "",
            "created_at": 2,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                ("same", "conv-main", "user", "same text", "[]", 2),
            )
            await db.commit()
        duplicate = await self.plan(
            operation("same", "message", "create", payload)
        )
        fresh_payload = dict(payload, id="fresh", text="new")
        fresh = await self.plan(
            operation(
                "fresh", "message", "create", fresh_payload, entity_id="fresh"
            )
        )
        self.assertEqual("duplicate", duplicate.rows[0].decision)
        self.assertEqual("apply", fresh.rows[0].decision)
        self.assertEqual(("insert_message",), fresh.rows[0].effects)

    async def test_missing_timeline_mapping_is_quarantined(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE homecoming_snapshot_exports "
                "SET timeline_mapping_json='{}'"
            )
            await db.commit()
        plan = await self.plan(
            operation(
                "message-one",
                "message",
                "create",
                {
                    "id": "message-one",
                    "timeline_id": "group",
                    "role": "assistant",
                    "sender_id": "main",
                    "text": "text",
                    "created_at": 2,
                },
            )
        )
        self.assertEqual("quarantine", plan.rows[0].decision)

    async def test_manual_memory_phone_wins_with_server_preimage_audit(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "memory-one", "server memory", "event", 1, "conv-main",
                    None, "server", 0.5, None, "active",
                ),
            )
            await db.commit()
        plan = await self.plan(
            operation(
                "memory-op",
                "memory",
                "update",
                {
                    "id": "memory-one",
                    "owner_id": "main",
                    "content": "phone memory",
                    "keywords": "phone",
                    "tombstone": False,
                    "updated_at": 2,
                },
                entity_id="memory-one",
                base_revision="old-phone-baseline",
            )
        )
        self.assertEqual("apply", plan.rows[0].decision)
        self.assertEqual(("upsert_memory", "audit_preimage"), plan.rows[0].effects)
        self.assertEqual(
            "server memory", plan.rows[0].effect_payload["server_preimage"]["content"]
        )

    async def test_auto_memory_requires_complete_source_messages(self):
        plan = await self.plan(
            operation(
                "auto-one",
                "memory_auto",
                "create",
                {
                    "id": "auto-one",
                    "owner_id": "main",
                    "content": "summary",
                    "source_message_ids": ["missing"],
                    "updated_at": 2,
                },
            )
        )
        self.assertEqual("skip", plan.rows[0].decision)

    async def test_summary_checkpoint_accepts_exact_planned_message_sources(self):
        first = operation(
            "message-one",
            "message",
            "create",
            {
                "id": "message-one",
                "epoch_id": "epoch-one",
                "timeline_id": "main_private",
                "role": "user",
                "sender_id": "user",
                "text": "one",
                "created_at": 2,
            },
            sequence=1,
        )
        second = operation(
            "message-two",
            "message",
            "create",
            {
                "id": "message-two",
                "epoch_id": "epoch-one",
                "timeline_id": "group",
                "role": "assistant",
                "sender_id": "main",
                "text": "two",
                "created_at": 3,
            },
            sequence=2,
        )

        plan = await self.plan(
            first,
            second,
            checkpoint_operation(3, ["message-one", "message-two"]),
        )

        self.assertEqual("apply", plan.rows[2].decision)
        self.assertEqual(("insert_summary_coverage",), plan.rows[2].effects)

    async def test_schedule_rules_are_history_only_or_server_first(self):
        execute = await self.plan(
            operation(
                "execute-one",
                "schedule",
                "execute",
                {"schedule_id": "schedule-one", "trigger_at": 1000},
            )
        )
        future = await self.plan(
            operation(
                "future",
                "schedule",
                "create",
                {
                    "id": "future",
                    "type": "reminder",
                    "trigger_at": 3_000_000,
                    "content": "future",
                    "owner_id": "main",
                    "timeline_id": "main_private",
                    "status": "active",
                    "updated_at": 2,
                },
                entity_id="future",
            ),
            now=2000,
        )
        past = await self.plan(
            operation(
                "past",
                "schedule",
                "create",
                {
                    "id": "past",
                    "type": "reminder",
                    "trigger_at": 1000,
                    "content": "past",
                    "owner_id": "main",
                    "timeline_id": "main_private",
                    "status": "active",
                    "updated_at": 2,
                },
                entity_id="past",
            ),
            now=2000,
        )
        self.assertEqual(("audit",), execute.rows[0].effects)
        self.assertEqual("apply", future.rows[0].decision)
        self.assertEqual("skip", past.rows[0].decision)

    async def test_supervision_is_audit_only_and_deferred_control_is_quarantined(self):
        supervision = await self.plan(
            operation(
                "event-one",
                "supervision_event",
                "execute",
                {"event_id": "event-one", "result_text": "done"},
            )
        )
        deferred = await self.plan(
            operation(
                "control-one",
                "deferred_control",
                "create",
                {"type": "unlock", "expires_at": 9999},
            )
        )
        self.assertEqual("apply", supervision.rows[0].decision)
        self.assertEqual(("audit",), supervision.rows[0].effects)
        self.assertEqual("quarantine", deferred.rows[0].decision)


if __name__ == "__main__":
    unittest.main()
