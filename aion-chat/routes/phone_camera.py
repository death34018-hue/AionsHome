"""Android phone-camera arming, status, and JPEG upload routes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from phone_camera import phone_camera


router = APIRouter()


class PhoneCameraArm(BaseModel):
    client_id: str
    facing: str = "back"
    zoom: float = 1.0
    capabilities: dict = Field(default_factory=dict)


class PhoneCameraDisarm(BaseModel):
    client_id: str = ""


class PhoneCameraFailure(BaseModel):
    request_id: str
    error: str = "phone_capture_failed"
    metadata: dict = Field(default_factory=dict)


@router.post("/api/phone-camera/arm")
async def arm_phone_camera(body: PhoneCameraArm):
    return phone_camera.arm(
        body.client_id,
        body.facing,
        body.zoom,
        body.capabilities,
    )


@router.post("/api/phone-camera/disarm")
async def disarm_phone_camera(body: PhoneCameraDisarm | None = None):
    return phone_camera.disarm(body.client_id if body else None)


@router.get("/api/phone-camera/status")
async def phone_camera_status():
    return phone_camera.status()


def accept_phone_camera_upload(
    request_id: str,
    jpeg: bytes,
    metadata: dict,
) -> dict:
    result = phone_camera.accept_upload(request_id, jpeg, metadata)
    return {
        "ok": result.status in ("ready", "duplicate"),
        "status": result.status,
        "request_id": result.request_id,
        "error": result.error,
        "received_at": result.received_at,
        "width": result.width,
        "height": result.height,
        "byte_size": result.byte_size,
    }


@router.post("/api/phone-camera/upload")
async def upload_phone_camera(request: Request):
    request_id = request.headers.get("x-phone-camera-request-id", "").strip()
    metadata_raw = request.headers.get("x-phone-camera-metadata", "").strip()
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {"metadata_error": "invalid_json"}
    return accept_phone_camera_upload(request_id, await request.body(), metadata)


@router.post("/api/phone-camera/failure")
async def fail_phone_camera_capture(body: PhoneCameraFailure):
    result = phone_camera.accept_failure(
        body.request_id,
        body.error,
        body.metadata,
    )
    return {
        "ok": result.status in ("failed", "duplicate"),
        "status": result.status,
        "request_id": result.request_id,
        "error": result.error,
    }


def accept_phone_camera_preview(jpeg: bytes, metadata: dict) -> dict:
    """Retain one explicit setup frame without satisfying a capture waiter."""
    payload = bytes(jpeg or b"")
    if not payload or len(payload) > phone_camera.max_upload_bytes:
        return {"ok": False, "status": "invalid_image"}
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return {"ok": False, "status": "invalid_image"}
    height, width = image.shape[:2]
    phone_camera.capture_dir.mkdir(parents=True, exist_ok=True)
    path = Path(phone_camera.capture_dir) / "phone_camera_preview_latest.jpg"
    temp = path.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(path)
    return {
        "ok": True,
        "status": "preview",
        "received_at": time.time(),
        "width": width,
        "height": height,
        "byte_size": len(payload),
        "metadata": dict(metadata if isinstance(metadata, dict) else {}),
    }


@router.post("/api/phone-camera/preview")
async def upload_phone_camera_preview(request: Request):
    metadata_raw = request.headers.get("x-phone-camera-metadata", "").strip()
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {}
    return accept_phone_camera_preview(await request.body(), metadata)
