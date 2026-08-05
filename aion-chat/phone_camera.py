"""Request-correlated Android phone camera capture coordination."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from config import DATA_DIR, UPLOADS_DIR


ALLOWED_CAPTURE_REASONS = frozenset({
    "sentinel_patrol",
    "scheduled_monitor",
    "ai_cam_check",
})
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 45.0
DEFAULT_RETRY_AFTER_SECONDS = 15.0
DEFAULT_MAX_UPLOAD_BYTES = 800 * 1024


@dataclass(frozen=True)
class PhoneCameraResult:
    status: str
    request_id: str = ""
    path: Path | None = None
    received_at: float = 0.0
    error: str = ""
    width: int = 0
    height: int = 0
    byte_size: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class _PendingCapture:
    request_id: str
    reason: str
    created_at: float
    deadline_at: float
    client_id: str
    facing: str
    zoom: float
    event: threading.Event = field(default_factory=threading.Event)
    result: PhoneCameraResult | None = None


class PhoneCameraCoordinator:
    def __init__(
        self,
        *,
        capture_dir: Path | None = None,
        upload_dir: Path | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        completed_limit: int = 128,
        retained_files: int = 64,
    ):
        self.capture_dir = Path(capture_dir or (DATA_DIR / "phone_camera"))
        self.upload_dir = Path(upload_dir or UPLOADS_DIR)
        self.max_upload_bytes = max(1, int(max_upload_bytes))
        self.completed_limit = max(8, int(completed_limit))
        self.retained_files = max(1, int(retained_files))
        self._lock = threading.RLock()
        self._armed = False
        self._client_id = ""
        self._facing = "back"
        self._zoom = 1.0
        self._capabilities: dict = {}
        self._armed_at = 0.0
        self._pending: dict[str, _PendingCapture] = {}
        self._completed: OrderedDict[str, PhoneCameraResult] = OrderedDict()
        self._last_result = PhoneCameraResult(status="never")

    @staticmethod
    def _normalize_facing(facing: str) -> str:
        value = str(facing or "").strip().lower()
        if value in ("front", "user"):
            return "front"
        if value in ("back", "environment"):
            return "back"
        raise ValueError("facing must be front or back")

    @staticmethod
    def _zoom_range(capabilities: dict, facing: str) -> tuple[float, float]:
        item = capabilities.get(facing) if isinstance(capabilities, dict) else None
        if not isinstance(item, dict):
            return 1.0, max(1.0, float("inf"))
        try:
            minimum = float(item.get("minZoom", 1.0))
            maximum = float(item.get("maxZoom", max(1.0, minimum)))
        except (TypeError, ValueError):
            return 1.0, float("inf")
        minimum = max(0.1, minimum)
        maximum = max(minimum, maximum)
        return minimum, maximum

    def arm(
        self,
        client_id: str,
        facing: str,
        zoom: float,
        capabilities: dict,
    ) -> dict:
        normalized_client = str(client_id or "").strip()[:160]
        if not normalized_client.startswith("android-push:"):
            raise ValueError("client_id must identify an Android push service")
        normalized_facing = self._normalize_facing(facing)
        safe_capabilities = capabilities if isinstance(capabilities, dict) else {}
        minimum, maximum = self._zoom_range(safe_capabilities, normalized_facing)
        try:
            requested_zoom = float(zoom)
        except (TypeError, ValueError):
            requested_zoom = 1.0
        applied_zoom = max(minimum, min(maximum, requested_zoom))
        with self._lock:
            self._armed = True
            self._client_id = normalized_client
            self._facing = normalized_facing
            self._zoom = applied_zoom
            self._capabilities = dict(safe_capabilities)
            self._armed_at = time.time()
            return self.status()

    def disarm(self, client_id: str | None = None) -> dict:
        with self._lock:
            if client_id and str(client_id).strip() != self._client_id:
                return self.status()
            self._armed = False
            self._armed_at = 0.0
            for pending in list(self._pending.values()):
                result = PhoneCameraResult(
                    status="unavailable",
                    request_id=pending.request_id,
                    error="phone_camera_disarmed",
                )
                pending.result = result
                pending.event.set()
                self._remember_completed(result)
            self._pending.clear()
            return self.status()

    def status(self) -> dict:
        with self._lock:
            last = self._last_result
            return {
                "armed": self._armed,
                "client_id": self._client_id if self._armed else "",
                "facing": self._facing,
                "zoom": self._zoom,
                "capabilities": dict(self._capabilities),
                "armed_at": self._armed_at,
                "pending_count": len(self._pending),
                "last_capture": {
                    "status": last.status,
                    "request_id": last.request_id,
                    "received_at": last.received_at,
                    "error": last.error,
                    "width": last.width,
                    "height": last.height,
                    "byte_size": last.byte_size,
                    "metadata": dict(last.metadata),
                },
            }

    def _create_pending(self, reason: str, timeout_seconds: float) -> _PendingCapture:
        if reason not in ALLOWED_CAPTURE_REASONS:
            raise ValueError(f"unsupported capture reason: {reason}")
        with self._lock:
            if not self._armed or not self._client_id:
                raise RuntimeError("phone camera is not armed")
            created_at = time.time()
            request_id = f"cam_{uuid.uuid4().hex}"
            pending = _PendingCapture(
                request_id=request_id,
                reason=reason,
                created_at=created_at,
                deadline_at=created_at + max(0.0, float(timeout_seconds)),
                client_id=self._client_id,
                facing=self._facing,
                zoom=self._zoom,
            )
            self._pending[request_id] = pending
            return pending

    @staticmethod
    def _command_event(pending: _PendingCapture) -> dict:
        return {
            "type": "phone_camera_capture",
            "data": {
                "request_id": pending.request_id,
                "reason": pending.reason,
                "created_at": pending.created_at,
                "deadline_at": pending.deadline_at,
                "facing": pending.facing,
                "zoom": pending.zoom,
            },
        }

    def _remember_completed(self, result: PhoneCameraResult) -> None:
        self._completed[result.request_id] = result
        self._completed.move_to_end(result.request_id)
        while len(self._completed) > self.completed_limit:
            self._completed.popitem(last=False)
        self._last_result = result

    def _finish_without_image(
        self,
        pending: _PendingCapture,
        status: str,
        error: str,
    ) -> PhoneCameraResult:
        with self._lock:
            current = self._pending.pop(pending.request_id, None)
            if current and current.result:
                return current.result
            result = PhoneCameraResult(
                status=status,
                request_id=pending.request_id,
                error=error,
            )
            if current:
                current.result = result
                current.event.set()
            self._remember_completed(result)
            return result

    async def request_capture(
        self,
        reason: str,
        send_command: Callable,
        *,
        timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        retry_after_seconds: float = DEFAULT_RETRY_AFTER_SECONDS,
    ) -> PhoneCameraResult:
        pending = self._create_pending(reason, timeout_seconds)
        event = self._command_event(pending)
        try:
            delivered = send_command(pending.client_id, event)
            if inspect.isawaitable(delivered):
                await delivered
            first_wait = min(
                max(0.0, float(retry_after_seconds)),
                max(0.0, float(timeout_seconds)),
            )
            completed = await asyncio.to_thread(pending.event.wait, first_wait)
            if not completed and first_wait < max(0.0, float(timeout_seconds)):
                delivered = send_command(pending.client_id, event)
                if inspect.isawaitable(delivered):
                    await delivered
                remaining = max(0.0, float(timeout_seconds) - first_wait)
                completed = await asyncio.to_thread(pending.event.wait, remaining)
            if completed and pending.result:
                return pending.result
            return self._finish_without_image(pending, "timeout", "capture_timeout")
        except asyncio.CancelledError:
            self._finish_without_image(pending, "cancelled", "request_cancelled")
            raise
        except Exception as error:
            return self._finish_without_image(
                pending,
                "unavailable",
                f"command_failed:{type(error).__name__}",
            )

    def request_capture_sync(
        self,
        reason: str,
        send_command_sync: Callable,
        *,
        timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        retry_after_seconds: float = DEFAULT_RETRY_AFTER_SECONDS,
        should_continue: Callable[[], bool] | None = None,
    ) -> PhoneCameraResult:
        if should_continue is not None and not should_continue():
            return PhoneCameraResult(status="cancelled", error="caller_cancelled")
        pending = self._create_pending(reason, timeout_seconds)
        event = self._command_event(pending)
        retry_at = time.monotonic() + max(0.0, float(retry_after_seconds))
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        retried = False
        try:
            send_command_sync(pending.client_id, event)
            while True:
                if should_continue is not None and not should_continue():
                    return self._finish_without_image(
                        pending, "cancelled", "caller_cancelled"
                    )
                if pending.event.wait(0.05) and pending.result:
                    return pending.result
                now = time.monotonic()
                if not retried and now >= retry_at and now < deadline:
                    send_command_sync(pending.client_id, event)
                    retried = True
                if now >= deadline:
                    return self._finish_without_image(
                        pending, "timeout", "capture_timeout"
                    )
        except Exception as error:
            return self._finish_without_image(
                pending,
                "unavailable",
                f"command_failed:{type(error).__name__}",
            )

    def accept_upload(
        self,
        request_id: str,
        jpeg: bytes,
        metadata: dict,
    ) -> PhoneCameraResult:
        normalized_id = str(request_id or "").strip()
        with self._lock:
            pending = self._pending.get(normalized_id)
            completed = self._completed.get(normalized_id)
            if completed:
                duplicate_status = (
                    "duplicate" if completed.status == "ready" else "expired"
                )
                return replace(completed, status=duplicate_status)
            if pending is None:
                return PhoneCameraResult(
                    status="unknown_request",
                    request_id=normalized_id,
                    error="unknown_request",
                )

        payload = bytes(jpeg or b"")
        if len(payload) > self.max_upload_bytes:
            return PhoneCameraResult(
                status="too_large",
                request_id=normalized_id,
                error="upload_too_large",
                byte_size=len(payload),
            )
        if not payload:
            return PhoneCameraResult(
                status="invalid_image",
                request_id=normalized_id,
                error="empty_upload",
            )
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return PhoneCameraResult(
                status="invalid_image",
                request_id=normalized_id,
                error="jpeg_decode_failed",
                byte_size=len(payload),
            )
        height, width = image.shape[:2]
        if width < 32 or height < 32:
            return PhoneCameraResult(
                status="invalid_image",
                request_id=normalized_id,
                error="image_dimensions_too_small",
                width=width,
                height=height,
                byte_size=len(payload),
            )

        received_at = time.time()
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized_id)[:80]
        filename = f"phone_camera_{safe_id}_{time.time_ns()}.jpg"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        capture_path = self.capture_dir / filename
        upload_path = self.upload_dir / filename
        capture_tmp = capture_path.with_suffix(".tmp")
        upload_tmp = upload_path.with_suffix(".tmp")
        capture_tmp.write_bytes(payload)
        upload_tmp.write_bytes(payload)
        os.replace(capture_tmp, capture_path)
        os.replace(upload_tmp, upload_path)

        result = PhoneCameraResult(
            status="ready",
            request_id=normalized_id,
            path=upload_path,
            received_at=received_at,
            width=width,
            height=height,
            byte_size=len(payload),
            metadata=dict(metadata if isinstance(metadata, dict) else {}),
        )
        with self._lock:
            current = self._pending.pop(normalized_id, None)
            if current is None:
                capture_path.unlink(missing_ok=True)
                upload_path.unlink(missing_ok=True)
                return PhoneCameraResult(
                    status="expired",
                    request_id=normalized_id,
                    error="request_no_longer_pending",
                )
            current.result = result
            self._remember_completed(result)
            current.event.set()
        self._cleanup_files()
        return result

    def accept_failure(
        self,
        request_id: str,
        error: str,
        metadata: dict,
    ) -> PhoneCameraResult:
        normalized_id = str(request_id or "").strip()
        with self._lock:
            completed = self._completed.get(normalized_id)
            if completed:
                return replace(completed, status="duplicate")
            pending = self._pending.pop(normalized_id, None)
            if pending is None:
                return PhoneCameraResult(
                    status="unknown_request",
                    request_id=normalized_id,
                    error="unknown_request",
                )
            result = PhoneCameraResult(
                status="failed",
                request_id=normalized_id,
                error=str(error or "phone_capture_failed")[:240],
                metadata=dict(metadata if isinstance(metadata, dict) else {}),
            )
            pending.result = result
            self._remember_completed(result)
            pending.event.set()
            return result

    def _cleanup_files(self) -> None:
        for directory in (self.capture_dir, self.upload_dir):
            try:
                files = sorted(
                    directory.glob("phone_camera_*.jpg"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
                for expired in files[self.retained_files:]:
                    expired.unlink(missing_ok=True)
            except Exception:
                pass


phone_camera = PhoneCameraCoordinator()
