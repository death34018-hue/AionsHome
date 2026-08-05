import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def message_operation(index, *, sequence=None):
    sequence = sequence or index
    message_id = f"phone-message-{index}"
    return {
        "op_id": f"op-{index}",
        "device_seq": sequence,
        "entity_type": "message",
        "entity_id": message_id,
        "action": "create",
        "base_revision": "",
        "payload": {
            "id": message_id,
            "timeline_id": "main_private",
            "role": "user",
            "sender_id": "user",
            "text": f"phone text {index}",
            "attachment_kind": "",
            "attachment_transcript": "",
            "created_at": 1000 + index,
        },
        "created_at": 1000 + index,
    }


def checkpoint_operation(sequence, source_ids):
    from homecoming.contracts import canonical_json_bytes, sha256_hex

    core = {
        "checkpoint_id": f"checkpoint-{sequence}",
        "epoch_id": "epoch-one",
        "owner_id": "main",
        "previous_message_id": "",
        "last_message_id": source_ids[-1],
        "source_message_ids": source_ids,
        "memory_ids": [],
    }
    return {
        "op_id": f"checkpoint-op-{sequence}",
        "device_seq": sequence,
        "entity_type": "summary_checkpoint",
        "entity_id": core["checkpoint_id"],
        "action": "create",
        "base_revision": "",
        "payload": dict(
            core, payload_sha256=sha256_hex(canonical_json_bytes(core))
        ),
        "created_at": 1000 + sequence,
    }


class ImporterFixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "importer.db"

    async def initialize(self):
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
                CREATE TABLE homecoming_devices (
                    device_id TEXT PRIMARY KEY
                );
                CREATE TABLE homecoming_snapshot_exports (
                    device_id TEXT, snapshot_id TEXT, etag TEXT,
                    manifest_json TEXT, timeline_mapping_json TEXT,
                    created_at REAL,
                    PRIMARY KEY (device_id, snapshot_id)
                );
                CREATE TABLE sync_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT, entity_type TEXT, entity_id TEXT,
                    payload TEXT, created_at REAL
                );
                """
            )
            await db.execute(
                "INSERT INTO homecoming_devices VALUES (?)",
                ("android:test-device",),
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

    def factory(self, *, timeout=1.0):
        return lambda: aiosqlite.connect(self.db_path, timeout=timeout)

    async def plan(self, operations, now=100):
        from homecoming.return_planner import plan_stored_package
        from homecoming.return_store import ensure_return_tables

        package_id = "return-" + ("%064x" % len(operations))
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_return_tables(db)
            await db.execute(
                "INSERT INTO homecoming_return_packages "
                "(package_id,device_id,epoch_id,base_snapshot_id,"
                "first_device_seq,highest_device_seq,operation_count,"
                "payload_sha256,signature_b64,quarantine_path,state,"
                "received_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    package_id,
                    "android:test-device",
                    "epoch-one",
                    "snapshot-one",
                    operations[0]["device_seq"],
                    operations[-1]["device_seq"],
                    len(operations),
                    package_id[7:],
                    "signature",
                    "fixture.gz",
                    "received",
                    now,
                    now,
                ),
            )
            for operation in operations:
                await db.execute(
                    "INSERT INTO homecoming_return_operations "
                    "(op_id,package_id,device_id,device_seq,entity_type,"
                    "entity_id,action,base_revision,payload_json,created_at,state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation["op_id"],
                        package_id,
                        "android:test-device",
                        operation["device_seq"],
                        operation["entity_type"],
                        operation["entity_id"],
                        operation["action"],
                        operation["base_revision"],
                        json.dumps(operation["payload"]),
                        operation["created_at"],
                        "received",
                    ),
                )
            plan = await plan_stored_package(db, package_id, now)
            await db.commit()
        return plan

    def close(self):
        self.tmp.cleanup()


class HomecomingReturnImporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = ImporterFixture()
        await self.fixture.initialize()

    async def asyncTearDown(self):
        self.fixture.close()

    async def test_second_apply_is_idempotent_and_receipt_is_stable(self):
        from homecoming.return_importer import apply_import_plan

        plan = await self.fixture.plan([message_operation(1)])
        first = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=200
        )
        second = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=201
        )
        self.assertTrue(first.complete)
        self.assertEqual(first.result_summary_sha256, second.result_summary_sha256)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE id='phone-message-1'"
            ).fetchone()[0]
        self.assertEqual(1, count)

    async def test_android_message_milliseconds_are_stored_as_server_seconds(self):
        from homecoming.return_importer import apply_import_plan

        operation = message_operation(1)
        operation["payload"]["timeline_id"] = "group"
        operation["payload"]["created_at"] = 1_785_160_990_373
        operation["created_at"] = 1_785_160_990_373

        plan = await self.fixture.plan([operation])
        receipt = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=1_785_161_100
        )

        self.assertTrue(receipt.complete)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            message_time = db.execute(
                "SELECT created_at FROM chatroom_messages "
                "WHERE id='phone-message-1'"
            ).fetchone()[0]
            room_time = db.execute(
                "SELECT updated_at FROM chatroom_rooms WHERE id='room-group'"
            ).fetchone()[0]
        self.assertEqual(1_785_160_990.373, message_time)
        self.assertEqual(1_785_160_990.373, room_time)

    async def test_verified_checkpoint_writes_owner_scoped_coverage(self):
        message = message_operation(1)
        checkpoint = checkpoint_operation(2, ["phone-message-1"])
        plan = await self.fixture.plan([message, checkpoint])

        from homecoming.return_importer import apply_import_plan

        receipt = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=200
        )

        self.assertTrue(receipt.complete)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            row = db.execute(
                "SELECT owner_id,message_id,epoch_id,checkpoint_id "
                "FROM homecoming_summary_coverage"
            ).fetchone()
        self.assertEqual(
            ("main", "phone-message-1", "epoch-one", "checkpoint-2"), row
        )

    async def test_mixed_apply_and_quarantine_never_executes_control(self):
        from homecoming.return_importer import apply_import_plan

        control = {
            "op_id": "control",
            "device_seq": 2,
            "entity_type": "deferred_control",
            "entity_id": "control",
            "action": "create",
            "base_revision": "",
            "payload": {"type": "unlock", "expires_at": 9999},
            "created_at": 2,
        }
        plan = await self.fixture.plan([message_operation(1), control])
        receipt = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=200
        )
        self.assertTrue(receipt.complete)
        self.assertEqual(1, receipt.counts["apply"])
        self.assertEqual(1, receipt.counts["quarantine"])

    async def test_fifty_row_boundary_resumes_from_committed_cursor(self):
        from homecoming.return_importer import apply_import_plan

        plan = await self.fixture.plan(
            [message_operation(index) for index in range(1, 52)]
        )
        first = await apply_import_plan(
            self.fixture.factory(),
            plan.plan_id,
            now=200,
            max_rows=50,
            clock=lambda: 0.0,
        )
        self.assertFalse(first.complete)
        self.assertEqual(50, first.accepted_highest_device_seq)
        second = await apply_import_plan(
            self.fixture.factory(),
            plan.plan_id,
            now=201,
            max_rows=50,
            clock=lambda: 0.0,
        )
        self.assertTrue(second.complete)
        self.assertEqual(51, second.accepted_highest_device_seq)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            self.assertEqual(
                51, db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            )

    async def test_time_budget_commits_at_least_one_row_then_yields(self):
        from homecoming.return_importer import apply_import_plan

        plan = await self.fixture.plan(
            [message_operation(1), message_operation(2)]
        )
        ticks = iter([0.0, 0.3, 0.3])
        receipt = await apply_import_plan(
            self.fixture.factory(),
            plan.plan_id,
            now=200,
            budget_ms=250,
            clock=lambda: next(ticks, 0.3),
        )
        self.assertFalse(receipt.complete)
        self.assertEqual(1, receipt.accepted_highest_device_seq)

    async def test_relevant_mainline_change_rejects_stale_plan(self):
        from homecoming.return_importer import StaleImportPlan, apply_import_plan

        plan = await self.fixture.plan([message_operation(1)])
        async with aiosqlite.connect(self.fixture.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (
                    "phone-message-1",
                    "conv-main",
                    "user",
                    "concurrent server value",
                    "[]",
                    1,
                ),
            )
            await db.commit()
        with self.assertRaises(StaleImportPlan):
            await apply_import_plan(
                self.fixture.factory(), plan.plan_id, now=200
            )

    async def test_sqlite_busy_returns_retryable_without_partial_write(self):
        from homecoming.return_importer import apply_import_plan

        plan = await self.fixture.plan([message_operation(1)])
        blocker = await aiosqlite.connect(self.fixture.db_path)
        try:
            await blocker.execute("BEGIN IMMEDIATE")
            receipt = await apply_import_plan(
                self.fixture.factory(timeout=0.01), plan.plan_id, now=200
            )
            self.assertTrue(receipt.retryable)
            self.assertFalse(receipt.complete)
        finally:
            await blocker.rollback()
            await blocker.close()
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            self.assertEqual(
                0,
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE id='phone-message-1'"
                ).fetchone()[0],
            )

    async def test_post_commit_broadcast_failure_cannot_undo_import(self):
        from homecoming.return_importer import apply_import_plan

        plan = await self.fixture.plan([message_operation(1)])

        async def failing_publisher(events):
            with closing(sqlite3.connect(self.fixture.db_path)) as db:
                self.assertEqual(
                    1,
                    db.execute(
                        "SELECT COUNT(*) FROM messages "
                        "WHERE id='phone-message-1'"
                    ).fetchone()[0],
                )
            raise RuntimeError("broadcast offline")

        receipt = await apply_import_plan(
            self.fixture.factory(),
            plan.plan_id,
            now=200,
            publisher=failing_publisher,
        )
        self.assertTrue(receipt.complete)
        again = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=201
        )
        self.assertEqual(receipt.result_summary_sha256,
                         again.result_summary_sha256)

    async def test_schedule_and_supervision_are_audit_only(self):
        from homecoming.return_importer import apply_import_plan

        schedule = {
            "op_id": "schedule-execute",
            "device_seq": 1,
            "entity_type": "schedule",
            "entity_id": "schedule-one",
            "action": "execute",
            "base_revision": "",
            "payload": {"schedule_id": "schedule-one", "trigger_at": 1000},
            "created_at": 1,
        }
        supervision = {
            "op_id": "supervision",
            "device_seq": 2,
            "entity_type": "supervision_event",
            "entity_id": "supervision",
            "action": "execute",
            "base_revision": "",
            "payload": {"event_id": "supervision", "result_text": "done"},
            "created_at": 2,
        }
        plan = await self.fixture.plan([schedule, supervision])
        receipt = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=200
        )
        self.assertTrue(receipt.complete)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
            )

    async def test_memory_future_schedule_and_group_message_use_sql_only(self):
        from homecoming.return_importer import apply_import_plan

        async with aiosqlite.connect(self.fixture.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                ("source-one", "conv-main", "user", "source", "[]", 1),
            )
            await db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "memory-one", "server", "event", 1, "conv-main",
                    None, "server", 0.5, None, "active",
                ),
            )
            await db.commit()
        operations = [
            {
                "op_id": "memory-update",
                "device_seq": 1,
                "entity_type": "memory",
                "entity_id": "memory-one",
                "action": "update",
                "base_revision": "old",
                "payload": {
                    "id": "memory-one",
                    "owner_id": "main",
                    "content": "phone",
                    "keywords": "phone",
                    "tombstone": False,
                    "updated_at": 2,
                },
                "created_at": 2,
            },
            {
                "op_id": "memory-auto",
                "device_seq": 2,
                "entity_type": "memory_auto",
                "entity_id": "memory-auto",
                "action": "create",
                "base_revision": "",
                "payload": {
                    "id": "memory-auto",
                    "owner_id": "main",
                    "content": "automatic",
                    "keywords": "auto",
                    "importance": 0.7,
                    "source_message_ids": ["source-one"],
                    "updated_at": 3,
                },
                "created_at": 3,
            },
            {
                "op_id": "schedule-future",
                "device_seq": 3,
                "entity_type": "schedule",
                "entity_id": "schedule-future",
                "action": "create",
                "base_revision": "",
                "payload": {
                    "id": "schedule-future",
                    "type": "reminder",
                    "trigger_at": 3_000_000,
                    "content": "future",
                    "owner_id": "main",
                    "timeline_id": "main_private",
                    "status": "active",
                    "updated_at": 4,
                },
                "created_at": 4,
            },
            {
                "op_id": "group-message",
                "device_seq": 4,
                "entity_type": "message",
                "entity_id": "group-message",
                "action": "create",
                "base_revision": "",
                "payload": {
                    "id": "group-message",
                    "timeline_id": "group",
                    "role": "assistant",
                    "sender_id": "main",
                    "text": "group text",
                    "attachment_kind": "",
                    "attachment_transcript": "",
                    "created_at": 5,
                },
                "created_at": 5,
            },
        ]
        plan = await self.fixture.plan(operations, now=100)
        receipt = await apply_import_plan(
            self.fixture.factory(), plan.plan_id, now=200
        )
        self.assertTrue(receipt.complete)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            self.assertEqual(
                "phone",
                db.execute(
                    "SELECT content FROM memories WHERE id='memory-one'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "automatic",
                db.execute(
                    "SELECT content FROM memories WHERE id='memory-auto'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                db.execute(
                    "SELECT COUNT(*) FROM schedules "
                    "WHERE id='schedule-future' AND status='active'"
                ).fetchone()[0],
            )
            self.assertEqual(
                ("aion", "group text"),
                db.execute(
                    "SELECT sender,content FROM chatroom_messages "
                    "WHERE id='group-message'"
                ).fetchone(),
            )


if __name__ == "__main__":
    unittest.main()
