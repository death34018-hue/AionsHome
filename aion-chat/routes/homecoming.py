"""Isolated export endpoints for the phone's Homecoming disaster mode."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from chatroom import load_chatroom_config
from config import MODELS, SETTINGS
from database import get_db
from homecoming.contracts import (
    HOMECOMING_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_hex,
)
from homecoming.crypto import (
    InvalidDevicePublicKey,
    build_portable_routes,
    encrypt_route_bundle,
)
from homecoming.devices import (
    DeviceKeyConflict,
    InvalidDeviceId,
    ensure_homecoming_tables,
    get_device,
    register_device,
)
from homecoming.snapshot import build_snapshot_sections
from homecoming.return_contracts import (
    MAX_COMPRESSED_BYTES,
    InvalidReturnPackage,
    return_package_device_id,
    verify_return_envelope,
)
from homecoming.return_store import (
    get_return_package_status,
    latest_import_plan_id,
    store_verified_package,
)
from homecoming.return_planner import plan_stored_package
from homecoming.return_importer import StaleImportPlan, apply_import_plan


router = APIRouter(prefix="/api/homecoming/v1", tags=["homecoming"])
RETURN_QUARANTINE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "homecoming_returns"
    / "quarantine"
)


class DeviceRegistration(BaseModel):
    device_id: str = Field(min_length=8, max_length=160)
    public_key_spki_b64: str = Field(min_length=128, max_length=8192)
    signing_public_key_spki_b64: str = Field(min_length=128, max_length=8192)


class SnapshotBuildRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=160)
    previous_snapshot_id: str | None = Field(default=None, max_length=128)


def _etag(snapshot_id: str) -> str:
    return f'"{snapshot_id}"'


def _etag_value(raw: str | None) -> str:
    value = str(raw or "").strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"')


def _manifest(snapshot_id: str, created_at: float, sections: dict, routes: dict) -> dict:
    timelines = sections.get("timelines") or {}
    memories = sections.get("memories") or {}
    return {
        "ready": True,
        "schema": HOMECOMING_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "main_memory_count": len(memories.get("main") or []),
        "second_memory_count": len(memories.get("second") or []),
        "main_message_count": len(
            (timelines.get("main_private") or {}).get("messages") or []
        ),
        "second_message_count": len(
            (timelines.get("companion_private") or {}).get("messages") or []
        ),
        "group_message_count": len(
            (timelines.get("group") or {}).get("messages") or []
        ),
        "pending_schedule_count": len(sections.get("schedules") or []),
        "portable_route_count": len(routes.get("chat") or []),
    }


@router.post("/devices/register")
async def register_homecoming_device(body: DeviceRegistration):
    try:
        async with get_db() as db:
            device = await register_device(
                db,
                body.device_id,
                body.public_key_spki_b64,
                body.signing_public_key_spki_b64,
                now=time.time(),
            )
            await db.commit()
    except (InvalidDeviceId, InvalidDevicePublicKey) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DeviceKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "device_id": device["device_id"],
        "public_key_sha256": device["public_key_sha256"],
        "signing_public_key_sha256": device["signing_public_key_sha256"],
        "registered_at": device["registered_at"],
        "last_seen_at": device["last_seen_at"],
    }


@router.post("/return-packages", status_code=202)
async def receive_homecoming_return_package(request: Request):
    raw = await _read_bounded_return_body(request)
    try:
        device_id = return_package_device_id(raw)
        async with get_db() as db:
            device = await get_device(db, device_id)
            if device is None:
                raise HTTPException(status_code=404, detail="device is not registered")
            if device["revoked_at"] is not None:
                raise HTTPException(status_code=403, detail="device is revoked")
            package = verify_return_envelope(raw, device)
            status = await store_verified_package(
                db, package, raw, RETURN_QUARANTINE_ROOT, time.time()
            )
            await db.commit()
            return status
    except InvalidDeviceId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidReturnPackage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="return operation is already bound"
        ) from exc


@router.get("/return-packages/{package_id}")
async def homecoming_return_package_status(package_id: str):
    if (
        len(package_id) != 71
        or not package_id.startswith("return-")
        or any(character not in "0123456789abcdef" for character in package_id[7:])
    ):
        raise HTTPException(status_code=422, detail="invalid return package id")
    async with get_db() as db:
        status = await get_return_package_status(db, package_id)
    if status is None:
        raise HTTPException(status_code=404, detail="return package was not found")
    return status


async def _read_bounded_return_body(request: Request) -> bytes:
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_COMPRESSED_BYTES:
            raise HTTPException(
                status_code=413, detail="compressed return package is too large"
            )
        chunks.extend(chunk)
    return bytes(chunks)


@router.get("/snapshot/manifest")
async def latest_homecoming_manifest(
    device_id: str = Query(min_length=8, max_length=160),
):
    try:
        async with get_db() as db:
            device = await get_device(db, device_id)
            if device is None or device["revoked_at"] is not None:
                raise HTTPException(status_code=404, detail="device is not registered")
            await ensure_homecoming_tables(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT manifest_json FROM homecoming_snapshot_exports "
                "WHERE device_id=? ORDER BY created_at DESC LIMIT 1",
                (device_id,),
            )
            row = await cursor.fetchone()
    except InvalidDeviceId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        return {"ready": False}
    try:
        manifest = json.loads(row["manifest_json"])
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=500, detail="stored manifest is invalid") from exc
    return manifest


@router.post("/snapshot/build")
async def build_homecoming_snapshot(
    body: SnapshotBuildRequest,
    if_none_match: str | None = Header(default=None),
):
    now = time.time()
    try:
        async with get_db() as db:
            device = await get_device(db, body.device_id)
            if device is None or device["revoked_at"] is not None:
                raise HTTPException(status_code=404, detail="device is not registered")

            sections = await build_snapshot_sections(db, now=now)
            route_bundle = build_portable_routes(
                dict(SETTINGS),
                dict(MODELS),
                load_chatroom_config(),
            )
            snapshot_id = sha256_hex(canonical_json_bytes({
                "schema": HOMECOMING_SCHEMA_VERSION,
                "sections": sections,
                "route_bundle_hash": sha256_hex(canonical_json_bytes(route_bundle)),
            }))
            etag = _etag(snapshot_id)
            if (
                body.previous_snapshot_id == snapshot_id
                or _etag_value(if_none_match) == snapshot_id
            ):
                await db.execute(
                    "UPDATE homecoming_devices SET last_seen_at=? WHERE device_id=?",
                    (now, body.device_id),
                )
                await db.commit()
                return Response(
                    status_code=304,
                    headers={"ETag": etag, "Cache-Control": "no-store"},
                )

            encrypted_routes = encrypt_route_bundle(
                route_bundle,
                device["public_key_spki_b64"],
                device_id=body.device_id,
                snapshot_id=snapshot_id,
            )
            section_hashes = {
                name: sha256_hex(canonical_json_bytes(value))
                for name, value in sections.items()
            }
            payload = {
                "schema": HOMECOMING_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "created_at": now,
                "sections": sections,
                "encrypted_routes": encrypted_routes,
                "section_hashes": section_hashes,
            }
            manifest = _manifest(snapshot_id, now, sections, route_bundle)
            await ensure_homecoming_tables(db)
            await db.execute(
                "INSERT OR REPLACE INTO homecoming_snapshot_exports "
                "(device_id,snapshot_id,etag,manifest_json,"
                "timeline_mapping_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    body.device_id,
                    snapshot_id,
                    etag,
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        _timeline_mapping(sections),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            await db.execute(
                "UPDATE homecoming_devices SET last_seen_at=? WHERE device_id=?",
                (now, body.device_id),
            )
            await db.commit()
    except InvalidDeviceId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    compressed = gzip.compress(canonical_json_bytes(payload), compresslevel=6)
    return Response(
        content=compressed,
        media_type="application/json",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "no-store",
            "ETag": etag,
            "X-Homecoming-Snapshot-Id": snapshot_id,
        },
    )


@router.post("/return-packages/{package_id}/dry-run")
async def dry_run_homecoming_return_package(package_id: str):
    try:
        async with get_db() as db:
            plan = await plan_stored_package(db, package_id, time.time())
            await db.commit()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return plan.to_dict()


@router.post("/return-packages/{package_id}/apply")
async def apply_homecoming_return_package(package_id: str):
    async with get_db() as db:
        plan_id = await latest_import_plan_id(db, package_id)
    if plan_id is None:
        raise HTTPException(
            status_code=409, detail="return package must be dry-run first"
        )
    try:
        receipt = await apply_import_plan(
            get_db, plan_id, time.time(), max_rows=50, budget_ms=250
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StaleImportPlan as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return receipt.to_dict()


def _timeline_mapping(sections: dict) -> dict:
    timelines = sections.get("timelines")
    if not isinstance(timelines, dict):
        return {}
    result = {}
    for name in ("main_private", "companion_private", "group"):
        timeline = timelines.get(name)
        containers = timeline.get("containers") if isinstance(timeline, dict) else None
        if not isinstance(containers, list):
            continue
        ids = [
            str(item.get("id") or "")
            for item in containers
            if isinstance(item, dict) and item.get("id")
        ]
        if ids:
            result[name] = ids[-1]
    return result
