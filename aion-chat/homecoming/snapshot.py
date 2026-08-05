"""Read-only builder for the phone's Homecoming disaster snapshot."""

from __future__ import annotations

import base64
import json
from typing import Any

from .contracts import MAX_MESSAGES_PER_TIMELINE, TEXT_WINDOW_SECONDS


_PORTABLE_SETTING_KEYS = {
    "temperature",
    "main_memory_model",
    "memory_model",
    "memory_recall_backend",
    "memory_extraction_enabled",
    "ai_prompt_capabilities",
    "sentinel_base_url",
    "sentinel_model",
    "embedding_base_url",
    "embedding_model",
}

_SECRET_SETTING_KEYS = {
    "gemini_key",
    "gemini_free_key",
    "siliconflow_key",
    "aipro_key",
    "openrouter_key",
    "sentinel_api_key",
    "embedding_api_key",
}


async def _rows(db, query: str, parameters: tuple = ()) -> list[dict]:
    cursor = await db.execute(query, parameters)
    result = await cursor.fetchall()
    columns = [item[0] for item in cursor.description or ()]
    return [dict(zip(columns, row)) for row in result]


async def _table_columns(db, table: str) -> set[str]:
    rows = await _rows(db, f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in rows}


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _attachment_kind(item: dict) -> str:
    raw = str(
        item.get("kind")
        or item.get("type")
        or item.get("content_type")
        or item.get("mime")
        or ""
    ).lower()
    if "image" in raw:
        return "image"
    if "audio" in raw or "voice" in raw:
        return "audio"
    if "video" in raw:
        return "video"
    return "file"


def normalize_attachments(value: Any) -> list[dict]:
    normalized: list[dict] = []
    for raw in _json_list(value):
        item = raw if isinstance(raw, dict) else {}
        portable = {"kind": _attachment_kind(item)}
        transcript = item.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            transcript = item.get("text")
        if isinstance(transcript, str) and transcript.strip():
            portable["transcript"] = transcript.strip()
        normalized.append(portable)
    return normalized


def _portable_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    return value


def _portable_row(row: dict) -> dict:
    return {key: _portable_value(value) for key, value in row.items()}


async def _timeline_messages(
    db,
    *,
    source: str,
    cutoff: float,
) -> list[dict]:
    if source == "main_private":
        query = """
            SELECT m.id, m.conv_id AS container_id, m.role AS sender,
                   m.content, m.attachments, m.created_at
            FROM messages m
            WHERE m.created_at >= ?
            ORDER BY m.created_at DESC
            LIMIT ?
        """
        parameters = (cutoff, MAX_MESSAGES_PER_TIMELINE)
    else:
        room_type = "connor_1v1" if source == "companion_private" else "group"
        query = """
            SELECT m.id, m.room_id AS container_id, m.sender,
                   m.content, m.attachments, m.created_at
            FROM chatroom_messages m
            JOIN chatroom_rooms r ON r.id=m.room_id
            WHERE r.type=? AND m.created_at >= ?
            ORDER BY m.created_at DESC
            LIMIT ?
        """
        parameters = (room_type, cutoff, MAX_MESSAGES_PER_TIMELINE)

    rows = await _rows(db, query, parameters)
    rows.reverse()
    return [
        {
            "id": row["id"],
            "container_id": row["container_id"],
            "sender": row["sender"],
            "content": row["content"] or "",
            "attachments": normalize_attachments(row.get("attachments")),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _memory_rows(db, table: str) -> list[dict]:
    columns = await _table_columns(db, table)
    where = (
        " WHERE COALESCE(archive_state, 'active')='active'"
        if "archive_state" in columns
        else ""
    )
    rows = await _rows(db, f"SELECT * FROM {table}{where} ORDER BY created_at ASC")
    return [_portable_row(row) for row in rows]


async def _metadata(db) -> tuple[list[dict], list[dict]]:
    conversations = await _rows(
        db,
        "SELECT id,title,model,created_at,updated_at "
        "FROM conversations ORDER BY updated_at ASC",
    )
    rooms = await _rows(
        db,
        "SELECT id,title,type,aion_persona,connor_persona,"
        "context_minutes,ai_chat_rounds,created_at,updated_at "
        "FROM chatroom_rooms WHERE type IN ('connor_1v1','group') "
        "ORDER BY updated_at ASC",
    )
    return conversations, rooms


def _public_route_descriptors(settings: dict) -> list[dict]:
    descriptors: list[dict] = []
    for raw in settings.get("custom_model_routes") or []:
        if not isinstance(raw, dict):
            continue
        base_url = str(raw.get("base_url") or "").strip()
        if not base_url.lower().startswith("https://"):
            continue
        models = []
        for model in raw.get("models") or []:
            if isinstance(model, str):
                models.append({"key": model, "model": model})
            elif isinstance(model, dict):
                model_id = str(model.get("model") or model.get("model_id") or "").strip()
                model_key = str(model.get("key") or model.get("name") or model_id).strip()
                if model_id and model_key:
                    models.append({
                        "key": model_key,
                        "model": model_id,
                        "vision": bool(model.get("vision", True)),
                        "audio": model.get("audio") is True,
                    })
        if models:
            descriptors.append({
                "route_id": str(raw.get("id") or raw.get("name") or "custom"),
                "label": str(raw.get("name") or "Custom cloud route"),
                "provider": "custom_openai",
                "base_url": base_url,
                "models": models,
            })
    return descriptors


def _load_defaults() -> dict:
    from app_supervision_ai import state_cache
    from chatroom import load_chatroom_config
    from config import SETTINGS, load_cam_config, load_worldbook
    from location import (
        load_location_config,
        load_location_status,
    )

    try:
        from config import DIGEST_ANCHOR_PATH
        digest_anchor = json.loads(DIGEST_ANCHOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        digest_anchor = {}
    supervision, _received_at = state_cache.read()
    return {
        "worldbook": load_worldbook(),
        "chatroom_config": load_chatroom_config(),
        "settings": dict(SETTINGS),
        "location_config": load_location_config(),
        "location_status": load_location_status(),
        "camera_config": load_cam_config(),
        "digest_anchor": digest_anchor,
        "supervision_snapshot": supervision,
    }


async def build_snapshot_sections(
    db,
    *,
    now: float,
    supervision_snapshot: dict | None = None,
    worldbook: dict | None = None,
    chatroom_config: dict | None = None,
    settings: dict | None = None,
    location_config: dict | None = None,
    location_status: dict | None = None,
    camera_config: dict | None = None,
    digest_anchor: dict | None = None,
) -> dict:
    defaults: dict = {}
    if any(value is None for value in (
        supervision_snapshot,
        worldbook,
        chatroom_config,
        settings,
        location_config,
        location_status,
        camera_config,
        digest_anchor,
    )):
        defaults = _load_defaults()

    worldbook = worldbook if worldbook is not None else defaults["worldbook"]
    chatroom_config = (
        chatroom_config
        if chatroom_config is not None
        else defaults["chatroom_config"]
    )
    settings = settings if settings is not None else defaults["settings"]
    location_config = (
        location_config
        if location_config is not None
        else defaults["location_config"]
    )
    location_status = (
        location_status
        if location_status is not None
        else defaults["location_status"]
    )
    camera_config = (
        camera_config
        if camera_config is not None
        else defaults["camera_config"]
    )
    digest_anchor = (
        digest_anchor
        if digest_anchor is not None
        else defaults["digest_anchor"]
    )
    supervision_snapshot = (
        supervision_snapshot
        if supervision_snapshot is not None
        else defaults["supervision_snapshot"]
    )

    cutoff = float(now) - TEXT_WINDOW_SECONDS
    conversations, rooms = await _metadata(db)
    timelines = {
        source: {
            "messages": await _timeline_messages(db, source=source, cutoff=cutoff),
        }
        for source in ("main_private", "companion_private", "group")
    }
    timelines["main_private"]["containers"] = conversations
    timelines["companion_private"]["containers"] = [
        room for room in rooms if room["type"] == "connor_1v1"
    ]
    timelines["group"]["containers"] = [
        room for room in rooms if room["type"] == "group"
    ]

    schedules = await _rows(
        db,
        "SELECT * FROM schedules WHERE status='active' ORDER BY trigger_at ASC",
    )
    portable_settings = {
        key: value
        for key, value in settings.items()
        if key in _PORTABLE_SETTING_KEYS and key not in _SECRET_SETTING_KEYS
    }

    return {
        "identity": {
            "user": {
                "name": worldbook.get("user_name") or "用户",
                "persona": worldbook.get("user_persona") or "",
            },
            "companions": {
                "main": {
                    "name": worldbook.get("ai_name") or "AI",
                    "persona": worldbook.get("ai_persona") or "",
                },
                "second": {
                    "name": chatroom_config.get("connor_name") or "第二AI",
                    "persona": chatroom_config.get("connor_persona") or "",
                },
            },
            "system_prompt": worldbook.get("system_prompt") or "",
            "reply_order": chatroom_config.get("reply_order") or "random",
            "tts": {
                "enabled": bool(chatroom_config.get("tts_enabled")),
                "main_voice": chatroom_config.get("tts_aion_voice") or "",
                "second_voice": chatroom_config.get("tts_connor_voice") or "",
            },
        },
        "memories": {
            "main": await _memory_rows(db, "memories"),
            "second": await _memory_rows(db, "chatroom_memories"),
        },
        "timelines": timelines,
        "schedules": [_portable_row(row) for row in schedules],
        "runtime_state": {
            "settings": portable_settings,
            "supervision": supervision_snapshot,
            "location": {
                "config": location_config,
                "status": location_status,
            },
            "camera": camera_config,
            "digest_anchor": digest_anchor,
        },
        "route_descriptors": _public_route_descriptors(settings),
    }
