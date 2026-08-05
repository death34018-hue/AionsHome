import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(spki).decode("ascii")


def _decrypt_envelope(envelope, private_key, *, device_id, snapshot_id):
    wrapped_key = base64.b64decode(envelope["wrapped_key_b64"])
    aes_key = private_key.decrypt(
        wrapped_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    aad = base64.b64decode(envelope["aad_b64"])
    expected_aad = json.dumps(
        {
            "device_id": device_id,
            "purpose": "aionshome-homecoming-routes",
            "schema": 1,
            "snapshot_id": snapshot_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if aad != expected_aad:
        raise ValueError("AAD does not match expected binding")
    plaintext = AESGCM(aes_key).decrypt(
        base64.b64decode(envelope["nonce_b64"]),
        base64.b64decode(envelope["ciphertext_b64"]),
        aad,
    )
    return json.loads(plaintext)


class HomecomingCryptoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "devices.db"
        self.private_key, self.spki = _key_pair()
        self.other_private_key, self.other_spki = _key_pair()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_same_device_and_key_registration_is_idempotent(self):
        from homecoming.devices import register_device

        async with aiosqlite.connect(self.db_path) as db:
            first = await register_device(
                db, "android:test-device", self.spki, now=10.0
            )
            second = await register_device(
                db, "android:test-device", self.spki, now=20.0
            )
            rows = await (
                await db.execute("SELECT COUNT(*) FROM homecoming_devices")
            ).fetchone()

        self.assertEqual(first["public_key_sha256"], second["public_key_sha256"])
        self.assertEqual(20.0, second["last_seen_at"])
        self.assertEqual(1, rows[0])

    async def test_same_device_with_different_key_is_rejected(self):
        from homecoming.devices import DeviceKeyConflict, register_device

        async with aiosqlite.connect(self.db_path) as db:
            await register_device(db, "android:test-device", self.spki, now=10.0)
            with self.assertRaises(DeviceKeyConflict):
                await register_device(
                    db, "android:test-device", self.other_spki, now=20.0
                )

    async def test_signing_key_can_be_filled_once_but_never_rebound(self):
        from homecoming.devices import DeviceKeyConflict, register_device

        async with aiosqlite.connect(self.db_path) as db:
            original = await register_device(
                db, "android:test-device", self.spki, now=10.0
            )
            self.assertEqual("", original["signing_public_key_sha256"])
            filled = await register_device(
                db,
                "android:test-device",
                self.spki,
                self.spki,
                now=20.0,
            )
            self.assertNotEqual("", filled["signing_public_key_sha256"])
            with self.assertRaises(DeviceKeyConflict):
                await register_device(
                    db,
                    "android:test-device",
                    self.spki,
                    self.other_spki,
                    now=30.0,
                )

    def test_route_filter_rejects_cli_local_and_insecure_routes(self):
        from homecoming.crypto import build_portable_routes

        routes = build_portable_routes(
            settings={
                "gemini_key": "gemini-secret",
                "siliconflow_key": "silicon-secret",
                "custom_model_routes": [
                    {
                        "id": "loopback",
                        "name": "Loopback",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "api_key": "loopback-secret",
                        "models": ["local-model"],
                    },
                    {
                        "id": "lan",
                        "name": "LAN",
                        "base_url": "https://192.168.1.20/v1",
                        "api_key": "lan-secret",
                        "models": ["lan-model"],
                    },
                    {
                        "id": "tailscale",
                        "name": "Tailscale",
                        "base_url": "https://100.64.0.10/v1",
                        "api_key": "tailscale-secret",
                        "models": ["tailscale-model"],
                    },
                ],
            },
            models={
                "CLI": {"provider": "codex_cli", "model": "gpt-cli"},
                "Gemini": {"provider": "gemini", "model": "gemini-cloud"},
            },
            chatroom_config={"connor_model": "CLI", "aion_model": "Gemini"},
        )
        serialized = json.dumps(routes, ensure_ascii=False)

        for forbidden in (
            "codex_cli",
            "loopback-secret",
            "lan-secret",
            "tailscale-secret",
            "127.0.0.1",
            "192.168.1.20",
            "100.64.0.10",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_route_filter_keeps_cloud_routes_and_typed_services(self):
        from homecoming.crypto import build_portable_routes

        routes = build_portable_routes(
            settings={
                "gemini_key": "gemini-secret",
                "gemini_free_key": "gemini-free-secret",
                "siliconflow_key": "silicon-secret",
                "sentinel_base_url": "https://sentinel.example/v1",
                "sentinel_api_key": "sentinel-secret",
                "sentinel_model": "sentinel-model",
                "embedding_base_url": "https://embedding.example/v1",
                "embedding_api_key": "embedding-secret",
                "embedding_model": "embedding-model",
                "custom_model_routes": [{
                    "id": "public-custom",
                    "name": "Fixture Cloud",
                    "base_url": "https://relay.example/v1",
                    "api_key": "custom-secret",
                    "models": [{
                        "key": "Fixture Vision",
                        "model": "vendor/vision",
                        "vision": True,
                    }],
                }],
            },
            models={
                "Gemini": {
                    "provider": "gemini",
                    "model": "gemini-cloud",
                    "vision": True,
                },
                "Silicon": {
                    "provider": "siliconflow",
                    "model": "silicon-cloud",
                    "vision": False,
                },
                "Custom": {
                    "provider": "custom_openai",
                    "model": "vendor/vision",
                    "base_url": "https://relay.example/v1",
                    "api_key": "custom-secret",
                    "route_id": "public-custom",
                    "route_name": "Fixture Cloud",
                    "vision": True,
                },
            },
            chatroom_config={
                "connor_model": "Custom",
                "aion_model": "Gemini",
                "tts_aion_voice": "voice-main",
                "tts_connor_voice": "voice-second",
                "tts_enabled": True,
            },
        )

        route_ids = {route["route_id"] for route in routes["chat"]}
        self.assertEqual(
            {"gemini", "siliconflow", "public-custom"}, route_ids
        )
        self.assertEqual(
            {"sentinel", "embedding", "tts"},
            set(routes["services"]),
        )
        self.assertEqual(
            "voice-second", routes["services"]["tts"]["second_voice"]
        )

    def test_envelope_decrypts_only_with_matching_key_and_binding(self):
        from homecoming.crypto import encrypt_route_bundle

        route_bundle = {
            "chat": [{
                "route_id": "public-custom",
                "api_key": "secret-test-key",
            }],
            "services": {},
        }
        envelope = encrypt_route_bundle(
            route_bundle,
            self.spki,
            device_id="android:test-device",
            snapshot_id="snap-test",
        )
        self.assertEqual(
            "RSA-OAEP-256-MGF1-SHA1+A256GCM",
            envelope["algorithm"],
        )

        decrypted = _decrypt_envelope(
            envelope,
            self.private_key,
            device_id="android:test-device",
            snapshot_id="snap-test",
        )
        self.assertEqual(route_bundle, decrypted)
        self.assertNotIn("secret-test-key", json.dumps(envelope))
        with self.assertRaises(ValueError):
            _decrypt_envelope(
                envelope,
                self.private_key,
                device_id="android:test-device",
                snapshot_id="snap-other",
            )

    def test_invalid_spki_is_rejected(self):
        from homecoming.crypto import InvalidDevicePublicKey, encrypt_route_bundle

        with self.assertRaises(InvalidDevicePublicKey):
            encrypt_route_bundle(
                {"chat": [], "services": {}},
                base64.b64encode(b"not-spki").decode("ascii"),
                device_id="android:test-device",
                snapshot_id="snap-test",
            )


if __name__ == "__main__":
    unittest.main()
