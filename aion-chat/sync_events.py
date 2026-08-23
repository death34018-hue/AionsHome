"""Durable cross-device event helpers.

Legacy WebSocket event names stay intact so older clients keep working.  A
monotonic ``sync_seq`` is added to the same payload, and reconnecting clients
can replay those exact messages through ``/api/sync/changes``.
"""

from __future__ import annotations

import json
import copy
import time
from typing import Any

from database import get_db


_ENTITY_BY_EVENT = {
    "conv_created": "conversation",
    "conv_updated": "conversation",
    "conv_deleted": "conversation",
    "msg_created": "message",
    "msg_updated": "message",
    "msg_deleted": "message",
    "chatroom_msg_created": "chatroom_message",
    "chatroom_msg_updated": "chatroom_message",
    "chatroom_msg_deleted": "chatroom_message",
    "chatroom_room_created": "chatroom_room",
    "chatroom_room_updated": "chatroom_room",
    "chatroom_room_deleted": "chatroom_room",
    "memory_added": "memory",
    "memory_updated": "memory",
    "memory_deleted": "memory",
    "memory_collection_changed": "memory",
    "moment_new": "moment",
    "moment_updated": "moment",
    "moment_deleted": "moment",
    "moment_comment": "moment_comment",
    "moment_comment_deleted": "moment_comment",
    "moment_reaction": "moment_reaction",
    "moment_reaction_removed": "moment_reaction",
    "chatroom_settings_updated": "chatroom_settings",
}


def is_sync_event_type(event_type: str) -> bool:
    return event_type in _ENTITY_BY_EVENT


def sanitize_sync_event(event: dict) -> dict:
    """Keep hidden persona metadata out of durable reconnect payloads."""
    cleaned = copy.deepcopy(event)
    data = cleaned.get("data") if isinstance(cleaned.get("data"), dict) else None
    if data is not None and isinstance(data.get("content"), str):
        from autonomy_state import strip_autonomy_state
        data["content"] = strip_autonomy_state(data["content"])
    return cleaned


def _entity_id(event_type: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("id", "message_id", "comment_id", "moment_id", "room_id", "conv_id"):
        value = data.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


async def append_sync_event(db, event: dict) -> int | None:
    """Append a supported event using the caller's transaction."""
    event = sanitize_sync_event(event)
    event_type = str(event.get("type") or "")
    entity_type = _ENTITY_BY_EVENT.get(event_type)
    if not entity_type:
        return None
    data = event.get("data")
    cursor = await db.execute(
        "INSERT INTO sync_events (event_type, entity_type, entity_id, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        (
            event_type,
            entity_type,
            _entity_id(event_type, data),
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            time.time(),
        ),
    )
    lastrowid = getattr(cursor, "lastrowid", None)
    return int(lastrowid) if lastrowid is not None else None


def attach_sync_seq(event: dict, seq: int | None) -> dict:
    payload = dict(event)
    payload["sync_seq"] = int(seq) if seq is not None else None
    return payload


async def persist_sync_event(event: dict) -> int | None:
    """Persist an event when the caller cannot share its write transaction."""
    async with get_db() as db:
        seq = await append_sync_event(db, event)
        if seq is not None:
            # Bound disk use while keeping a generous reconnect window.
            if seq % 100 == 0:
                await db.execute(
                    "DELETE FROM sync_events WHERE seq <= "
                    "COALESCE((SELECT MAX(seq) - 20000 FROM sync_events), 0)"
                )
            await db.commit()
        return seq


async def broadcast_synced(broadcaster, event: dict, seq: int | None = None) -> dict:
    """Broadcast once, preserving the legacy type and adding a durable cursor."""
    if seq is None:
        try:
            seq = await persist_sync_event(event)
        except Exception:
            # Startup migrations and isolated tests may briefly precede the
            # table.  Live delivery still works; foreground refresh is the
            # final safety net until the table is available.
            seq = None
    payload = attach_sync_seq(event, seq)
    await broadcaster.broadcast(payload)
    return payload
