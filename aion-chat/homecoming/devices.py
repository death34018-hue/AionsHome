"""Homecoming-only device registry stored beside the main database."""

from __future__ import annotations

import hashlib
import re

from .crypto import public_key_der


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")


class InvalidDeviceId(ValueError):
    pass


class DeviceKeyConflict(RuntimeError):
    pass


async def ensure_homecoming_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_devices (
            device_id TEXT PRIMARY KEY,
            public_key_spki_b64 TEXT NOT NULL,
            public_key_sha256 TEXT NOT NULL,
            signing_public_key_spki_b64 TEXT NOT NULL DEFAULT '',
            signing_public_key_sha256 TEXT NOT NULL DEFAULT '',
            registered_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            revoked_at REAL
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(homecoming_devices)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "signing_public_key_spki_b64" not in columns:
        await db.execute(
            "ALTER TABLE homecoming_devices ADD COLUMN "
            "signing_public_key_spki_b64 TEXT NOT NULL DEFAULT ''"
        )
    if "signing_public_key_sha256" not in columns:
        await db.execute(
            "ALTER TABLE homecoming_devices ADD COLUMN "
            "signing_public_key_sha256 TEXT NOT NULL DEFAULT ''"
        )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_snapshot_exports (
            device_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            etag TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            timeline_mapping_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            PRIMARY KEY (device_id, snapshot_id),
            FOREIGN KEY (device_id) REFERENCES homecoming_devices(device_id)
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(homecoming_snapshot_exports)")
    export_columns = {row[1] for row in await cursor.fetchall()}
    if "timeline_mapping_json" not in export_columns:
        await db.execute(
            "ALTER TABLE homecoming_snapshot_exports ADD COLUMN "
            "timeline_mapping_json TEXT NOT NULL DEFAULT '{}'"
        )


def _validate_device_id(device_id: str) -> str:
    normalized = str(device_id or "").strip()
    if not _DEVICE_ID_RE.fullmatch(normalized):
        raise InvalidDeviceId("invalid device id")
    return normalized


async def get_device(db, device_id: str) -> dict | None:
    normalized = _validate_device_id(device_id)
    await ensure_homecoming_tables(db)
    cursor = await db.execute(
        "SELECT device_id,public_key_spki_b64,public_key_sha256,"
        "signing_public_key_spki_b64,signing_public_key_sha256,"
        "registered_at,last_seen_at,revoked_at "
        "FROM homecoming_devices WHERE device_id=?",
        (normalized,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "device_id": row[0],
        "public_key_spki_b64": row[1],
        "public_key_sha256": row[2],
        "signing_public_key_spki_b64": row[3],
        "signing_public_key_sha256": row[4],
        "registered_at": row[5],
        "last_seen_at": row[6],
        "revoked_at": row[7],
    }


async def register_device(
    db,
    device_id: str,
    public_key_spki_b64: str,
    signing_public_key_spki_b64: str = "",
    *,
    now: float,
) -> dict:
    normalized = _validate_device_id(device_id)
    der = public_key_der(public_key_spki_b64)
    fingerprint = hashlib.sha256(der).hexdigest()
    signing_fingerprint = ""
    if signing_public_key_spki_b64:
        signing_der = public_key_der(signing_public_key_spki_b64)
        signing_fingerprint = hashlib.sha256(signing_der).hexdigest()
    await ensure_homecoming_tables(db)
    existing = await get_device(db, normalized)
    if existing is not None:
        if existing["revoked_at"] is not None:
            raise DeviceKeyConflict("device registration is revoked")
        if existing["public_key_sha256"] != fingerprint:
            raise DeviceKeyConflict("device public key does not match binding")
        if signing_fingerprint:
            existing_signing = existing["signing_public_key_sha256"]
            if existing_signing and existing_signing != signing_fingerprint:
                raise DeviceKeyConflict("device signing key does not match binding")
            if not existing_signing:
                await db.execute(
                    "UPDATE homecoming_devices SET "
                    "signing_public_key_spki_b64=?,signing_public_key_sha256=? "
                    "WHERE device_id=?",
                    (
                        signing_public_key_spki_b64,
                        signing_fingerprint,
                        normalized,
                    ),
                )
        await db.execute(
            "UPDATE homecoming_devices SET last_seen_at=? WHERE device_id=?",
            (float(now), normalized),
        )
    else:
        await db.execute(
            "INSERT INTO homecoming_devices "
            "(device_id,public_key_spki_b64,public_key_sha256,"
            "signing_public_key_spki_b64,signing_public_key_sha256,"
            "registered_at,last_seen_at,revoked_at) "
            "VALUES (?,?,?,?,?,?,?,NULL)",
            (
                normalized,
                public_key_spki_b64,
                fingerprint,
                signing_public_key_spki_b64,
                signing_fingerprint,
                float(now),
                float(now),
            ),
        )
    device = await get_device(db, normalized)
    if device is None:
        raise RuntimeError("device registration was not persisted")
    return device
