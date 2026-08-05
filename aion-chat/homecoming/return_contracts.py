"""Strict wire contract for signed Homecoming return packages."""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .contracts import canonical_json_bytes, sha256_hex


MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_OPERATION_BYTES = 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_PACKAGE_ID = re.compile(r"^return-[0-9a-f]{64}$")
_ALLOWED = {
    ("message", "create"),
    ("memory", "create"),
    ("memory", "update"),
    ("memory", "delete"),
    ("memory_auto", "create"),
    ("summary_checkpoint", "create"),
    ("schedule", "create"),
    ("schedule", "delete"),
    ("schedule", "execute"),
    ("supervision_event", "execute"),
    ("deferred_control", "create"),
}
_FORBIDDEN_MEDIA_KEYS = {
    "image",
    "image_url",
    "audio",
    "audio_url",
    "video",
    "video_url",
    "file",
    "file_path",
    "blob",
    "bytes",
    "binary",
    "camera_frame",
    "media_data",
}


class InvalidReturnPackage(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedOperation:
    op_id: str
    device_seq: int
    entity_type: str
    entity_id: str
    action: str
    base_revision: str
    payload: dict
    created_at: float


@dataclass(frozen=True)
class VerifiedPackage:
    package_id: str
    device_id: str
    epoch_id: str
    base_snapshot_id: str
    first_device_seq: int
    highest_device_seq: int
    operation_count: int
    payload_sha256: str
    signature_b64: str
    payload: dict
    operations: tuple[VerifiedOperation, ...]


def verify_return_envelope(raw_gzip: bytes, device: dict) -> VerifiedPackage:
    if not isinstance(raw_gzip, bytes) or not raw_gzip:
        raise InvalidReturnPackage("return package is empty")
    if len(raw_gzip) > MAX_COMPRESSED_BYTES:
        raise InvalidReturnPackage("compressed return package is too large")
    expanded = _bounded_gunzip(raw_gzip)
    try:
        envelope = json.loads(expanded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidReturnPackage("return envelope is invalid JSON") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise InvalidReturnPackage("return envelope must contain an object payload")
    payload = envelope["payload"]
    if payload.get("schema") != 1:
        raise InvalidReturnPackage("unsupported return package schema")

    package_id = str(envelope.get("package_id") or "")
    digest = str(envelope.get("payload_sha256") or "")
    if not _PACKAGE_ID.fullmatch(package_id) or package_id != "return-" + digest:
        raise InvalidReturnPackage("return package id does not match payload hash")
    canonical = canonical_json_bytes(payload)
    if sha256_hex(canonical) != digest:
        raise InvalidReturnPackage("return package payload hash mismatch")
    if envelope.get("signature_algorithm") != "SHA256withRSA/PSS":
        raise InvalidReturnPackage("unsupported return signature algorithm")

    device_id = _safe_id(payload.get("device_id"), "device id")
    epoch_id = _safe_id(payload.get("epoch_id"), "epoch id")
    base_snapshot_id = _safe_id(payload.get("base_snapshot_id"), "snapshot id")
    if device_id != device.get("device_id"):
        raise InvalidReturnPackage("return package device binding mismatch")
    signing_key = str(device.get("signing_public_key_spki_b64") or "")
    if not signing_key:
        raise InvalidReturnPackage("device has no registered return signing key")
    signature_b64 = str(envelope.get("signature_b64") or "")
    _verify_signature(
        signing_key,
        signature_b64,
        (
            "schema=1\n"
            f"device_id={device_id}\n"
            f"epoch_id={epoch_id}\n"
            f"payload_sha256={digest}\n"
        ).encode("utf-8"),
    )

    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise InvalidReturnPackage("return package operations are required")
    if len(raw_operations) > 100_000:
        raise InvalidReturnPackage("return package has too many operations")
    operations = tuple(_operation(item) for item in raw_operations)
    sequences = [item.device_seq for item in operations]
    if sequences[0] <= 0 or sequences != list(range(sequences[0], sequences[-1] + 1)):
        raise InvalidReturnPackage("return operation sequence is not contiguous")
    if payload.get("first_device_seq") != sequences[0]:
        raise InvalidReturnPackage("first device sequence does not match operations")
    if payload.get("highest_device_seq") != sequences[-1]:
        raise InvalidReturnPackage("highest device sequence does not match operations")
    if payload.get("operation_count") != len(operations):
        raise InvalidReturnPackage("operation count does not match operations")
    expected_counts: dict[str, int] = {}
    for operation in operations:
        expected_counts[operation.entity_type] = (
            expected_counts.get(operation.entity_type, 0) + 1
        )
    if payload.get("section_counts") != expected_counts:
        raise InvalidReturnPackage("section counts do not match operations")
    return VerifiedPackage(
        package_id=package_id,
        device_id=device_id,
        epoch_id=epoch_id,
        base_snapshot_id=base_snapshot_id,
        first_device_seq=sequences[0],
        highest_device_seq=sequences[-1],
        operation_count=len(operations),
        payload_sha256=digest,
        signature_b64=signature_b64,
        payload=payload,
        operations=operations,
    )


def return_package_device_id(raw_gzip: bytes) -> str:
    if not isinstance(raw_gzip, bytes) or not raw_gzip:
        raise InvalidReturnPackage("return package is empty")
    if len(raw_gzip) > MAX_COMPRESSED_BYTES:
        raise InvalidReturnPackage("compressed return package is too large")
    try:
        envelope = json.loads(_bounded_gunzip(raw_gzip))
        payload = envelope["payload"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidReturnPackage("return envelope is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidReturnPackage("return envelope must contain an object payload")
    return _safe_id(payload.get("device_id"), "device id")


def _bounded_gunzip(raw: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            expanded = stream.read(MAX_EXPANDED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise InvalidReturnPackage("return package gzip is invalid") from exc
    if len(expanded) > MAX_EXPANDED_BYTES:
        raise InvalidReturnPackage("expanded return package is too large")
    return expanded


def _operation(raw: object) -> VerifiedOperation:
    if not isinstance(raw, dict):
        raise InvalidReturnPackage("return operation must be an object")
    entity_type = str(raw.get("entity_type") or "")
    action = str(raw.get("action") or "")
    if (entity_type, action) not in _ALLOWED:
        raise InvalidReturnPackage("unsupported return operation")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise InvalidReturnPackage("return operation payload must be an object")
    if len(canonical_json_bytes(payload)) > MAX_OPERATION_BYTES:
        raise InvalidReturnPackage("return operation payload is too large")
    _reject_media(payload)
    try:
        sequence = int(raw.get("device_seq"))
        created_at = float(raw.get("created_at"))
    except (TypeError, ValueError) as exc:
        raise InvalidReturnPackage("return operation metadata is invalid") from exc
    return VerifiedOperation(
        op_id=_safe_id(raw.get("op_id"), "operation id"),
        device_seq=sequence,
        entity_type=entity_type,
        entity_id=_safe_id(raw.get("entity_id"), "entity id"),
        action=action,
        base_revision=str(raw.get("base_revision") or ""),
        payload=payload,
        created_at=created_at,
    )


def _reject_media(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if (
                normalized in _FORBIDDEN_MEDIA_KEYS
                or normalized.endswith("_b64")
                or normalized.endswith("_bytes")
            ):
                raise InvalidReturnPackage("binary media is forbidden in return packages")
            _reject_media(item)
    elif isinstance(value, list):
        for item in value:
            _reject_media(item)
    elif isinstance(value, str) and value.lower().startswith("data:"):
        raise InvalidReturnPackage("embedded data is forbidden in return packages")


def _safe_id(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise InvalidReturnPackage(f"invalid {label}")
    return normalized


def _verify_signature(public_key_b64: str, signature_b64: str, message: bytes) -> None:
    try:
        key = serialization.load_der_public_key(
            base64.b64decode(public_key_b64, validate=True)
        )
        signature = base64.b64decode(signature_b64, validate=True)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("invalid RSA signing key")
        key.verify(
            signature,
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise InvalidReturnPackage("return package signature is invalid") from exc
