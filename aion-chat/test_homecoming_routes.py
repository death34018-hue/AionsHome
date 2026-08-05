import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _public_spki():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


class HomecomingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "routes.db"
        self.spki = _public_spki()
        self.sections = {
            "identity": {
                "user": {"name": "Configured User"},
                "companions": {
                    "main": {"name": "Configured Main"},
                    "second": {"name": "Configured Second"},
                },
            },
            "memories": {
                "main": [{"id": "memory-main"}],
                "second": [{"id": "memory-second"}],
            },
            "timelines": {
                "main_private": {
                    "messages": [{"id": "main-message"}],
                    "containers": [{"id": "conv-main"}],
                },
                "companion_private": {
                    "messages": [],
                    "containers": [{"id": "room-second"}],
                },
                "group": {
                    "messages": [],
                    "containers": [{"id": "room-group"}],
                },
            },
            "schedules": [{"id": "schedule-one"}],
            "runtime_state": {},
            "route_descriptors": [],
        }
        self.route_bundle = {
            "chat": [{
                "route_id": "fixture-cloud",
                "label": "Fixture Cloud",
                "provider": "custom_openai",
                "base_url": "https://relay.example/v1",
                "api_key": "fixture-secret-key",
                "models": [{
                    "key": "fixture-model",
                    "model": "vendor/model",
                    "vision": True,
                    "audio": False,
                }],
            }],
            "services": {},
        }

        from routes import homecoming as route_module

        def connect():
            return aiosqlite.connect(self.db_path)

        self.patches = [
            patch.object(route_module, "get_db", connect),
            patch.object(
                route_module,
                "build_snapshot_sections",
                AsyncMock(return_value=self.sections),
            ),
            patch.object(
                route_module,
                "build_portable_routes",
                return_value=self.route_bundle,
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()
        app = FastAPI()
        app.include_router(route_module.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tmp.cleanup()

    def register(self, device_id="android:test-device"):
        return self.client.post(
            "/api/homecoming/v1/devices/register",
            json={
                "device_id": device_id,
                "public_key_spki_b64": self.spki,
                "signing_public_key_spki_b64": self.spki,
            },
        )

    def build(self, device_id="android:test-device", headers=None):
        return self.client.post(
            "/api/homecoming/v1/snapshot/build",
            json={
                "device_id": device_id,
                "previous_snapshot_id": None,
            },
            headers=headers or {},
        )

    def test_register_rejects_invalid_device_and_spki(self):
        response = self.client.post(
            "/api/homecoming/v1/devices/register",
            json={"device_id": "short", "public_key_spki_b64": "bad"},
        )
        self.assertEqual(422, response.status_code)

    def test_manifest_is_not_ready_before_first_build(self):
        self.assertEqual(200, self.register().status_code)
        response = self.client.get(
            "/api/homecoming/v1/snapshot/manifest",
            params={"device_id": "android:test-device"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ready": False}, response.json())

    def test_build_rejects_unregistered_device(self):
        response = self.build(device_id="android:unknown-device")
        self.assertEqual(404, response.status_code)

    def test_build_returns_hashes_and_never_plaintext_secrets(self):
        self.assertEqual(200, self.register().status_code)
        response = self.build()
        payload = response.json()

        self.assertEqual(200, response.status_code)
        self.assertEqual("gzip", response.headers["Content-Encoding"])
        self.assertEqual(
            payload["snapshot_id"],
            response.headers["X-Homecoming-Snapshot-Id"],
        )
        self.assertEqual(1, payload["schema"])
        self.assertEqual(
            set(self.sections),
            set(payload["section_hashes"]),
        )
        self.assertNotIn("fixture-secret-key", response.text)
        self.assertEqual(
            "RSA-OAEP-256-MGF1-SHA1+A256GCM",
            payload["encrypted_routes"]["algorithm"],
        )

    def test_unchanged_snapshot_returns_304(self):
        self.assertEqual(200, self.register().status_code)
        first = self.build()
        etag = first.headers["ETag"]
        response = self.build(headers={"If-None-Match": etag})

        self.assertEqual(304, response.status_code)
        self.assertEqual(b"", response.content)

    def test_latest_manifest_contains_counts_but_no_secret(self):
        self.assertEqual(200, self.register().status_code)
        self.assertEqual(200, self.build().status_code)
        response = self.client.get(
            "/api/homecoming/v1/snapshot/manifest",
            params={"device_id": "android:test-device"},
        )
        payload = response.json()

        self.assertTrue(payload["ready"])
        self.assertEqual(1, payload["main_memory_count"])
        self.assertEqual(1, payload["second_memory_count"])
        self.assertEqual(1, payload["pending_schedule_count"])
        self.assertEqual(1, payload["portable_route_count"])
        self.assertNotIn("fixture-secret-key", response.text)

    def test_build_persists_frozen_timeline_mapping_for_return(self):
        self.assertEqual(200, self.register().status_code)
        self.assertEqual(200, self.build().status_code)
        db = sqlite3.connect(self.db_path)
        try:
            raw = db.execute(
                "SELECT timeline_mapping_json "
                "FROM homecoming_snapshot_exports"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(
            {
                "main_private": "conv-main",
                "companion_private": "room-second",
                "group": "room-group",
            },
            json.loads(raw),
        )


if __name__ == "__main__":
    unittest.main()
