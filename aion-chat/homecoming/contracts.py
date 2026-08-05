"""Stable wire-format helpers for Homecoming snapshots."""

from __future__ import annotations

import hashlib
import json


HOMECOMING_SCHEMA_VERSION = 1
TEXT_WINDOW_SECONDS = 90 * 24 * 60 * 60
MAX_MESSAGES_PER_TIMELINE = 3000
SECTION_NAMES = (
    "identity",
    "memories",
    "timelines",
    "schedules",
    "runtime_state",
    "route_descriptors",
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
