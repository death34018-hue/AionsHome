import base64
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import aiosqlite
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "return.db"
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.spki = base64.b64encode(
            self.private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TABLE messages("
                "id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,"
                "attachments TEXT,created_at REAL)"
            )
            db.execute(
                "CREATE TABLE conversations("
                "id TEXT PRIMARY KEY,title TEXT,model TEXT,"
                "created_at REAL,updated_at REAL)"
            )
            db.execute(
                "INSERT INTO conversations VALUES "
                "('conv-main','Main','model',1,1)"
            )
            db.execute(
                "CREATE TABLE sync_events("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT,"
                "entity_type TEXT,entity_id TEXT,payload TEXT,created_at REAL)"
            )
            db.execute(
                "INSERT INTO messages VALUES "
                "('mainline-marker','conv-main','user','untouched','[]',1)"
            )
            db.commit()

        from routes import homecoming as route_module

        def connect():
            return aiosqlite.connect(self.db_path)

        self.patches = [
            patch.object(route_module, "get_db", connect),
            patch.object(
                route_module,
                "RETURN_QUARANTINE_ROOT",
                self.root / "quarantine",
            ),
        ]
        for active in self.patches:
            active.start()
        app = FastAPI()
        app.include_router(route_module.router)
        self.client = TestClient(app)
        response = self.client.post(
            "/api/homecoming/v1/devices/register",
            json={
                "device_id": "android:test-device",
                "public_key_spki_b64": self.spki,
                "signing_public_key_spki_b64": self.spki,
            },
        )
        assert response.status_code == 200, response.text

    def close(self):
        self.client.close()
        for active in reversed(self.patches):
            active.stop()
        self.tmp.cleanup()

    def package(self, operations=None, *, epoch="epoch-one", mutate=None):
        operations = operations or [{
            "op_id": "op-one",
            "device_seq": 1,
            "entity_type": "message",
            "entity_id": "message-one",
            "action": "create",
            "base_revision": "",
            "payload": {
                "id": "message-one",
                "timeline_id": "main_private",
                "role": "user",
                "sender_id": "user",
                "text": "归巢消息",
                "attachment_kind": "",
                "attachment_transcript": "",
                "created_at": 1000,
            },
            "created_at": 1000,
        }]
        counts = {}
        for item in operations:
            counts[item["entity_type"]] = counts.get(item["entity_type"], 0) + 1
        payload = {
            "schema": 1,
            "device_id": "android:test-device",
            "epoch_id": epoch,
            "base_snapshot_id": "snapshot-one",
            "created_at": 1000,
            "first_device_seq": operations[0]["device_seq"],
            "highest_device_seq": operations[-1]["device_seq"],
            "operation_count": len(operations),
            "operations": operations,
            "section_counts": counts,
        }
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        signing_text = (
            "schema=1\n"
            "device_id=android:test-device\n"
            f"epoch_id={epoch}\n"
            f"payload_sha256={digest}\n"
        ).encode()
        signature = self.private.sign(
            signing_text,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        envelope = {
            "package_id": "return-" + digest,
            "payload": payload,
            "payload_sha256": digest,
            "signature_algorithm": "SHA256withRSA/PSS",
            "signature_b64": base64.b64encode(signature).decode(),
        }
        if mutate:
            mutate(envelope)
        return gzip.compress(canonical(envelope), mtime=0)


@pytest.fixture
def fixture():
    value = Fixture()
    try:
        yield value
    finally:
        value.close()


def test_valid_package_is_quarantined_without_touching_mainline(fixture):
    response = fixture.client.post(
        "/api/homecoming/v1/return-packages",
        content=fixture.package(),
        headers={"Content-Type": "application/gzip"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "received"
    with closing(sqlite3.connect(fixture.db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM homecoming_return_packages"
        ).fetchone()[0] == 1
    stored = list((fixture.root / "quarantine").glob("*.json.gz"))
    assert len(stored) == 1


def test_same_package_is_idempotent_and_status_can_be_read(fixture):
    raw = fixture.package()
    first = fixture.client.post("/api/homecoming/v1/return-packages", content=raw)
    second = fixture.client.post("/api/homecoming/v1/return-packages", content=raw)
    status = fixture.client.get(
        f"/api/homecoming/v1/return-packages/{first.json()['package_id']}"
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == first.json()
    assert status.status_code == 200
    assert status.json()["state"] == "received"


@pytest.mark.parametrize(
    "bad_package",
    [
        lambda f: f.package(mutate=lambda e: e.update(signature_b64="AAAA")),
        lambda f: f.package([
            {
                "op_id": "op-one", "device_seq": 1,
                "entity_type": "camera_frame", "entity_id": "frame",
                "action": "create", "base_revision": "",
                "payload": {}, "created_at": 1,
            }
        ]),
        lambda f: f.package([
            {
                "op_id": "op-one", "device_seq": 1,
                "entity_type": "message", "entity_id": "message-one",
                "action": "create", "base_revision": "",
                "payload": {"image_b64": "AAAA"}, "created_at": 1,
            },
            {
                "op_id": "op-three", "device_seq": 3,
                "entity_type": "message", "entity_id": "message-three",
                "action": "create", "base_revision": "",
                "payload": {}, "created_at": 3,
            },
        ]),
        lambda f: f.package(epoch="../escape"),
    ],
)
def test_invalid_signature_operation_media_gap_and_traversal_are_rejected(
    fixture, bad_package
):
    response = fixture.client.post(
        "/api/homecoming/v1/return-packages", content=bad_package(fixture)
    )
    assert response.status_code == 422
    with closing(sqlite3.connect(fixture.db_path)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='homecoming_return_packages'"
        ).fetchone()
        if table:
            assert db.execute(
                "SELECT COUNT(*) FROM homecoming_return_packages"
            ).fetchone()[0] == 0


def test_revoked_device_cannot_upload(fixture):
    with closing(sqlite3.connect(fixture.db_path)) as db:
        db.execute(
            "UPDATE homecoming_devices SET revoked_at=1 WHERE device_id=?",
            ("android:test-device",),
        )
        db.commit()
    response = fixture.client.post(
        "/api/homecoming/v1/return-packages", content=fixture.package()
    )
    assert response.status_code == 403


def test_server_rejects_old_unfilled_sequence_gap(fixture):
    operation = {
        "op_id": "op-three",
        "device_seq": 3,
        "entity_type": "message",
        "entity_id": "message-three",
        "action": "create",
        "base_revision": "",
        "payload": {},
        "created_at": 3,
    }
    response = fixture.client.post(
        "/api/homecoming/v1/return-packages",
        content=fixture.package([operation]),
    )
    assert response.status_code == 422
    assert "expected 1" in response.text


def test_compressed_and_expanded_limits_fail_before_storage(fixture):
    from homecoming.return_contracts import (
        MAX_COMPRESSED_BYTES,
        return_package_device_id,
    )

    with pytest.raises(ValueError, match="compressed"):
        return_package_device_id(b"x" * (MAX_COMPRESSED_BYTES + 1))
    expanded_bomb = gzip.compress(b"x" * (32 * 1024 * 1024 + 1), mtime=0)
    with pytest.raises(ValueError, match="expanded"):
        return_package_device_id(expanded_bomb)
    with pytest.raises(ValueError, match="JSON"):
        return_package_device_id(gzip.compress(os.urandom(32), mtime=0))


def test_dry_run_is_persisted_and_deterministic(fixture):
    control = {
        "op_id": "control-one",
        "device_seq": 1,
        "entity_type": "deferred_control",
        "entity_id": "control-one",
        "action": "create",
        "base_revision": "",
        "payload": {"type": "unlock", "expires_at": 9999},
        "created_at": 1,
    }
    received = fixture.client.post(
        "/api/homecoming/v1/return-packages",
        content=fixture.package([control]),
    )
    package_id = received.json()["package_id"]
    first = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
    )
    second = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
    )
    assert first.status_code == 200, first.text
    assert second.json() == first.json()
    assert first.json()["rows"][0]["decision"] == "quarantine"
    with closing(sqlite3.connect(fixture.db_path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM homecoming_import_sessions"
        ).fetchone()[0] == 1


def test_relevant_mainline_change_stales_old_plan(fixture):
    with closing(sqlite3.connect(fixture.db_path)) as db:
        db.execute(
            "INSERT INTO homecoming_snapshot_exports "
            "(device_id,snapshot_id,etag,manifest_json,"
            "timeline_mapping_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                "android:test-device",
                "snapshot-one",
                '"snapshot-one"',
                "{}",
                '{"main_private":"conv-main"}',
                1,
            ),
        )
        db.commit()
    received = fixture.client.post(
        "/api/homecoming/v1/return-packages",
        content=fixture.package(),
    )
    package_id = received.json()["package_id"]
    first = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
    )
    assert first.json()["rows"][0]["decision"] == "apply"

    with closing(sqlite3.connect(fixture.db_path)) as db:
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            (
                "message-one",
                "conv-main",
                "user",
                "归巢消息",
                "[]",
                1000,
            ),
        )
        db.commit()
    second = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
    )
    assert second.json()["rows"][0]["decision"] == "duplicate"
    assert second.json()["plan_id"] != first.json()["plan_id"]
    with closing(sqlite3.connect(fixture.db_path)) as db:
        states = [
            row[0]
            for row in db.execute(
                "SELECT state FROM homecoming_import_sessions ORDER BY created_at"
            ).fetchall()
        ]
    assert sorted(states) == ["planned", "stale"]


def test_apply_route_imports_once_and_returns_stable_receipt(fixture):
    with closing(sqlite3.connect(fixture.db_path)) as db:
        db.execute(
            "INSERT INTO homecoming_snapshot_exports "
            "(device_id,snapshot_id,etag,manifest_json,"
            "timeline_mapping_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                "android:test-device",
                "snapshot-one",
                '"snapshot-one"',
                "{}",
                '{"main_private":"conv-main"}',
                1,
            ),
        )
        db.commit()
    received = fixture.client.post(
        "/api/homecoming/v1/return-packages",
        content=fixture.package(),
    )
    package_id = received.json()["package_id"]
    planned = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/dry-run"
    )
    assert planned.json()["rows"][0]["decision"] == "apply"
    first = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/apply"
    )
    second = fixture.client.post(
        f"/api/homecoming/v1/return-packages/{package_id}/apply"
    )
    assert first.status_code == 200, first.text
    assert first.json()["complete"] is True
    assert first.json()["result_summary_sha256"]
    assert second.json() == first.json()
    status = fixture.client.get(
        f"/api/homecoming/v1/return-packages/{package_id}"
    ).json()
    assert status["complete"] is True
    assert status["result_summary_sha256"] == first.json()["result_summary_sha256"]
    with closing(sqlite3.connect(fixture.db_path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE id='message-one'"
        ).fetchone()[0] == 1
