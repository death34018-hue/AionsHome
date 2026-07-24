"""Reliable AI-to-Mi-Band vibration command delivery."""

from __future__ import annotations

import hashlib
import re
import time

import aiosqlite

from capabilities import is_capability_enabled
from database import get_db
from ws import manager


BAND_VIBRATE_PATTERN = re.compile(r"\[BAND_VIBRATE:(single|call)\]", re.IGNORECASE)
BAND_NOTE_PATTERN = re.compile(
    r"\[BAND_NOTE_(SINGLE|CALL)\s*[：:]\s*([^\]]*)\]",
    re.IGNORECASE,
)
BAND_COMMAND_PATTERN = re.compile(
    r"\[BAND_NOTE_(SINGLE|CALL)\s*[：:]\s*([^\]]*)\]"
    r"|\[BAND_VIBRATE:(single|call)\]",
    re.IGNORECASE,
)
BAND_COMMAND_TTL_SECONDS = 600.0
MAX_BAND_NOTE_CODEPOINTS = 80
MAX_BAND_SENDER_CODEPOINTS = 30
BAND_VIBRATION_LABELS = {
    "single": "手环震动：轻轻想了你一下",
    "call": "手环震动：紧急呼叫！",
}


def _normalize_short_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def resolve_band_sender_name(sender: str) -> str:
    """Resolve an internal sender identity through the project's name configuration."""
    try:
        from chatroom import get_chatroom_names

        _user_name, ai_name, connor_name = get_chatroom_names()
        resolved = connor_name if (sender or "").lower() == "connor" else ai_name
    except Exception:
        resolved = "AI"
    return _normalize_short_text(resolved, MAX_BAND_SENDER_CODEPOINTS) or "AI"


def extract_band_vibration(text: str, enabled: bool = True) -> tuple[str, str | None, str]:
    """Hide every protocol marker and return at most the first executable command."""
    source = text or ""
    pattern = None
    note = ""
    if enabled:
        for match in BAND_COMMAND_PATTERN.finditer(source):
            if match.group(1):
                candidate_note = _normalize_short_text(
                    match.group(2), MAX_BAND_NOTE_CODEPOINTS
                )
                if not candidate_note:
                    continue
                pattern = match.group(1).lower()
                note = candidate_note
                break
            pattern = match.group(3).lower()
            break
    cleaned = BAND_COMMAND_PATTERN.sub("", source).strip()
    return cleaned, pattern, note


def band_vibration_attachment(
    pattern: str,
    *,
    note: str = "",
    sender_name: str = "",
) -> dict:
    """Build display-only metadata that never enters the message body."""
    normalized = (pattern or "").lower()
    if normalized not in BAND_VIBRATION_LABELS:
        raise ValueError("unsupported band vibration pattern")
    result = {
        "type": "band_vibration",
        "pattern": normalized,
    }
    clean_note = _normalize_short_text(note, MAX_BAND_NOTE_CODEPOINTS)
    if clean_note:
        result.update({
            "note": clean_note,
            "sender_name": _normalize_short_text(
                sender_name, MAX_BAND_SENDER_CODEPOINTS
            ),
            "label": (
                f"手环轻震：{clean_note}"
                if normalized == "single"
                else f"手环呼唤：{clean_note}"
            ),
        })
    else:
        result["label"] = BAND_VIBRATION_LABELS[normalized]
    return result


async def with_band_vibration_attachment(source_msg_id: str, attachments: list | None) -> list:
    """Attach a persisted vibration note to its source message, without mutating input."""
    result = list(attachments or [])
    if not source_msg_id:
        return result
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT pattern,note,sender_name FROM health_miband_commands "
            "WHERE source_msg_id=? ORDER BY created_at DESC LIMIT 1",
            (source_msg_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return result
    pattern, note_text, sender_name = row
    note = band_vibration_attachment(
        pattern, note=note_text or "", sender_name=sender_name or ""
    )
    result = [
        item for item in result
        if not (isinstance(item, dict) and item.get("type") == "band_vibration")
    ]
    result.append(note)
    return result


def _command_id(
    source_type: str,
    source_id: str,
    source_msg_id: str,
    pattern: str,
    note: str,
    sender_name: str,
) -> str:
    identity = "\x1f".join(
        (source_type, source_id, source_msg_id, pattern, note, sender_name)
    )
    return "band_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


async def enqueue_band_vibration(
    pattern: str,
    *,
    source_type: str,
    source_id: str,
    source_msg_id: str,
    note: str = "",
    sender_name: str = "",
    now: float | None = None,
) -> tuple[dict, bool]:
    if pattern not in {"single", "call"}:
        raise ValueError("unsupported band vibration pattern")
    created_at = time.time() if now is None else float(now)
    clean_note = _normalize_short_text(note, MAX_BAND_NOTE_CODEPOINTS)
    clean_sender = _normalize_short_text(sender_name, MAX_BAND_SENDER_CODEPOINTS)
    command_id = _command_id(
        source_type, source_id, source_msg_id, pattern, clean_note, clean_sender
    )
    command = {
        "id": command_id,
        "action": "vibrate",
        "pattern": pattern,
        "source_type": source_type,
        "source_id": source_id,
        "source_msg_id": source_msg_id,
        "note": clean_note,
        "sender_name": clean_sender,
        "created_at": created_at,
        "expires_at": created_at + BAND_COMMAND_TTL_SECONDS,
    }
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO health_miband_commands "
            "(id,pattern,source_type,source_id,source_msg_id,note,sender_name,created_at,expires_at,acknowledged_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (
                command_id,
                pattern,
                source_type,
                source_id,
                source_msg_id,
                command["note"],
                command["sender_name"],
                command["created_at"],
                command["expires_at"],
            ),
        )
        created = cursor.rowcount > 0
        await db.commit()
    return command, created


async def process_band_vibration(
    text: str,
    *,
    source_type: str,
    source_id: str,
    source_msg_id: str,
    sender: str = "aion",
) -> str:
    cleaned, pattern, note = extract_band_vibration(
        text,
        enabled=is_capability_enabled("band_vibration"),
    )
    if not pattern:
        return cleaned
    sender_name = resolve_band_sender_name(sender) if note else ""
    command, created = await enqueue_band_vibration(
        pattern,
        source_type=source_type,
        source_id=source_id,
        source_msg_id=source_msg_id,
        note=note,
        sender_name=sender_name,
    )
    if created:
        await manager.broadcast({"type": "mi_band_command", "data": command})
    return cleaned


async def list_pending_band_commands(now: float | None = None) -> list[dict]:
    current = time.time() if now is None else float(now)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id,pattern,source_type,source_id,source_msg_id,note,sender_name,created_at,expires_at "
            "FROM health_miband_commands "
            "WHERE acknowledged_at IS NULL AND expires_at>? ORDER BY created_at ASC",
            (current,),
        )
        rows = await cursor.fetchall()
    return [
        {
            **dict(row),
            "action": "vibrate",
        }
        for row in rows
    ]


async def acknowledge_band_command(command_id: str, now: float | None = None) -> bool:
    acknowledged_at = time.time() if now is None else float(now)
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE health_miband_commands SET acknowledged_at=? "
            "WHERE id=? AND acknowledged_at IS NULL AND expires_at>?",
            (acknowledged_at, command_id, acknowledged_at),
        )
        await db.commit()
        return cursor.rowcount > 0
