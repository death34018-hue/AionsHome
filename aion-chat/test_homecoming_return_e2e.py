import json
import sqlite3
from contextlib import closing

from test_homecoming_return_receive import Fixture


def operation(sequence, entity_type, entity_id, action, payload):
    return {
        "op_id": f"e2e-op-{sequence}",
        "device_seq": sequence,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "base_revision": "snapshot-one",
        "payload": payload,
        "created_at": 1000 + sequence,
    }


def message(sequence, message_id, timeline, role, sender, text):
    return operation(
        sequence,
        "message",
        message_id,
        "create",
        {
            "id": message_id,
            "timeline_id": timeline,
            "role": role,
            "sender_id": sender,
            "text": text,
            "attachment_kind": "",
            "attachment_transcript": "",
            "created_at": 1000 + sequence,
        },
    )


def prepare_mainline(fixture):
    with closing(sqlite3.connect(fixture.db_path)) as db:
        db.executescript(
            """
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
            """
        )
        db.executemany(
            "INSERT INTO chatroom_rooms VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("room-second", "Second", "connor_1v1", "", "", 30, 1, 1, 10),
                ("room-group", "Group", "group", "", "", 30, 3, 1, 10),
            ],
        )
        db.execute(
            "INSERT INTO homecoming_snapshot_exports "
            "(device_id,snapshot_id,etag,manifest_json,"
            "timeline_mapping_json,created_at) VALUES (?,?,?,?,?,?)",
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
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            ("main-user", "conv-main", "user", "already here", "[]", 1001),
        )
        db.executemany(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "memory-update", "server update", "event", 1, "conv-main",
                    None, "server", 0.5, None, "active",
                ),
                (
                    "memory-delete", "server delete", "event", 1, "conv-main",
                    None, "server", 0.5, None, "active",
                ),
                (
                    "memory-collision", "server wins", "event", 1, "conv-main",
                    None, "server", 0.5, None, "active",
                ),
                (
                    "memory-auto", "server summary", "event", 1, "conv-main",
                    None, "server", 0.5, "main-user", "active",
                ),
            ],
        )
        db.commit()


def representative_operations():
    values = [
        message(1, "main-user", "main_private", "user", "user", "already here"),
        message(2, "main-assistant", "main_private", "assistant", "main", "reply"),
        message(3, "second-user", "companion_private", "user", "user", "hello"),
        message(
            4, "second-assistant", "companion_private",
            "assistant", "second", "hi",
        ),
        message(5, "group-user", "group", "user", "user", "group hello"),
        message(6, "group-assistant", "group", "assistant", "main", "group reply"),
        operation(
            7, "memory", "memory-create", "create",
            {
                "id": "memory-create", "owner_id": "main",
                "content": "new memory", "keywords": "new",
                "importance": 0.6, "updated_at": 1007,
            },
        ),
        operation(
            8, "memory", "memory-update", "update",
            {
                "id": "memory-update", "owner_id": "main",
                "content": "phone update", "keywords": "phone",
                "importance": 0.7, "updated_at": 1008,
            },
        ),
        operation(
            9, "memory", "memory-delete", "delete",
            {
                "id": "memory-delete", "owner_id": "main",
                "content": "", "keywords": "", "updated_at": 1009,
            },
        ),
        operation(
            10, "memory", "memory-collision", "create",
            {
                "id": "memory-collision", "owner_id": "main",
                "content": "phone collision", "keywords": "phone",
                "updated_at": 1010,
            },
        ),
        operation(
            11, "memory_auto", "memory-auto", "create",
            {
                "id": "memory-auto", "owner_id": "main",
                "content": "phone summary", "keywords": "summary",
                "source_message_ids": ["main-user"], "updated_at": 1011,
            },
        ),
        operation(
            12, "schedule", "schedule-future", "create",
            {
                "id": "schedule-future", "type": "reminder",
                "trigger_at": 4_000_000_000, "content": "future",
                "owner_id": "main", "timeline_id": "main_private",
                "status": "active", "updated_at": 1012,
            },
        ),
        operation(
            13, "schedule", "schedule-past", "execute",
            {"schedule_id": "schedule-past", "trigger_at": 900},
        ),
        operation(
            14, "supervision_event", "supervision-one", "execute",
            {"event_id": "supervision-one", "result_text": "checked"},
        ),
        operation(
            15, "deferred_control", "control-one", "create",
            {"type": "unlock", "expires_at": 9999},
        ),
    ]
    return values


def main_table_counts(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "messages",
                "chatroom_messages",
                "memories",
                "chatroom_memories",
                "schedules",
            )
        }


def test_full_signed_return_is_classified_imported_and_idempotent():
    fixture = Fixture()
    try:
        prepare_mainline(fixture)
        raw = fixture.package(representative_operations(), epoch="epoch-e2e")

        received = fixture.client.post(
            "/api/homecoming/v1/return-packages",
            content=raw,
            headers={"Content-Type": "application/gzip"},
        )
        assert received.status_code == 202, received.text
        package_id = received.json()["package_id"]

        planned = fixture.client.post(
            f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
        )
        assert planned.status_code == 200, planned.text
        assert planned.json()["counts"] == {
            "apply": 11,
            "duplicate": 1,
            "server_wins": 1,
            "skip": 1,
            "quarantine": 1,
        }

        applied = fixture.client.post(
            f"/api/homecoming/v1/return-packages/{package_id}/apply"
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["complete"] is True
        assert applied.json()["accepted_highest_device_seq"] == 15
        assert applied.json()["result_summary_sha256"]

        before_repeat = main_table_counts(fixture.db_path)
        duplicate_upload = fixture.client.post(
            "/api/homecoming/v1/return-packages",
            content=raw,
            headers={"Content-Type": "application/gzip"},
        )
        duplicate_apply = fixture.client.post(
            f"/api/homecoming/v1/return-packages/{package_id}/apply"
        )
        assert duplicate_upload.status_code == 202
        assert duplicate_upload.json()["state"] == "confirmed"
        assert duplicate_apply.json() == applied.json()
        assert main_table_counts(fixture.db_path) == before_repeat
    finally:
        fixture.close()
