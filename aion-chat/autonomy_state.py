"""Persistent per-actor autonomy configuration and compact state packets."""

from __future__ import annotations

import json
import copy
import random
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

import aiosqlite

from config import DB_PATH


ACTOR_IDS = ("aion", "connor")
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60
MAX_STATE_CHARS = 800
ACTION_IDS = (
    "rest",
    "private_chat",
    "role_chat",
    "home_dynamics",
    "memory_browse",
    "album_browse",
    "web_roam",
    "xhs_roam",
    "taobao_roam",
    "friend_visit",
    "seeky_interaction",
    "wish_pool",
)

_AUTONOMY_WAKE_ACTOR = ContextVar("autonomy_wake_actor", default="")


def wake_summary_timeline_title(title: str, actor_name: str, metadata: dict | None) -> str | None:
    """Return the one objective timeline entry retained for a wake closeout."""
    if (metadata or {}).get("session_id"):
        return str(title or "").strip() or None
    selected_action = str((metadata or {}).get("selected_action") or "")
    if selected_action == "rest":
        return str(title or "").strip() or None
    if selected_action == "private_chat":
        return f"{actor_name}说了一句话"
    return None


_STATE_BLOCK_RE = re.compile(
    r"\s*<autonomy_state>\s*(.*?)\s*</autonomy_state>\s*",
    re.IGNORECASE | re.DOTALL,
)
_STATE_OPEN_RE = re.compile(r"\s*<autonomy_state>\s*", re.IGNORECASE)
_STATE_MARKER_RE = re.compile(r"\s*<autonomy_state\b", re.IGNORECASE)


def normalize_actor(actor: str) -> str:
    value = str(actor or "").strip().lower()
    if value not in ACTOR_IDS:
        raise ValueError("unknown autonomy actor")
    return value


def _clamp_minutes(value: Any, fallback: int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = fallback
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))


def _default_actions() -> dict[str, bool]:
    return {key: key != "album_browse" for key in ACTION_IDS}


async def ensure_autonomy_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_actor_configs (
            actor_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            min_interval_minutes INTEGER NOT NULL DEFAULT 5,
            max_interval_minutes INTEGER NOT NULL DEFAULT 1440,
            relationship_started_on TEXT,
            actions_json TEXT NOT NULL DEFAULT '{}',
            timer_started_at REAL,
            next_wake_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    cur = await db.execute("PRAGMA table_info(autonomy_actor_configs)")
    config_columns = {str(row[1]) for row in await cur.fetchall()}
    if "timer_started_at" not in config_columns:
        await db.execute("ALTER TABLE autonomy_actor_configs ADD COLUMN timer_started_at REAL")
    if "next_wake_at" not in config_columns:
        await db.execute("ALTER TABLE autonomy_actor_configs ADD COLUMN next_wake_at REAL")
    if "relationship_started_on" not in config_columns:
        await db.execute("ALTER TABLE autonomy_actor_configs ADD COLUMN relationship_started_on TEXT")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_state_packets (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            previous_id TEXT DEFAULT '',
            source TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            wake_at REAL,
            status TEXT NOT NULL,
            last_action TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_packets_actor_created "
        "ON autonomy_state_packets(actor_id, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_packets_due "
        "ON autonomy_state_packets(status, wake_at)"
    )
    now = time.time()
    defaults = json.dumps(_default_actions(), ensure_ascii=False)
    for actor in ACTOR_IDS:
        await db.execute(
            "INSERT OR IGNORE INTO autonomy_actor_configs "
            "(actor_id,enabled,min_interval_minutes,max_interval_minutes,actions_json,created_at,updated_at) "
            "VALUES(?,0,5,1440,?,?,?)",
            (actor, defaults, now, now),
        )
    await db.commit()


def _decode_actions(raw: str | None) -> dict[str, bool]:
    try:
        stored = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        stored = {}
    return {key: bool(stored.get(key, key != "album_browse")) for key in ACTION_IDS}


def _config_from_row(row) -> dict:
    return {
        "actor": row["actor_id"],
        "enabled": bool(row["enabled"]),
        "min_interval_minutes": int(row["min_interval_minutes"]),
        "max_interval_minutes": int(row["max_interval_minutes"]),
        "relationship_started_on": row["relationship_started_on"] or None,
        "actions": _decode_actions(row["actions_json"]),
        "timer_started_at": float(row["timer_started_at"]) if row["timer_started_at"] is not None else None,
        "next_wake_at": float(row["next_wake_at"]) if row["next_wake_at"] is not None else None,
        "updated_at": float(row["updated_at"]),
    }


async def get_actor_config(actor: str, db=None) -> dict:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cur = await db.execute(
            "SELECT * FROM autonomy_actor_configs WHERE actor_id=?", (actor,)
        )
        return _config_from_row(await cur.fetchone())
    finally:
        if owns_db:
            await db.close()


async def update_actor_config(
    actor: str,
    *,
    enabled: bool | None = None,
    min_interval_minutes: int | None = None,
    max_interval_minutes: int | None = None,
    relationship_started_on: str | None = None,
    actions: dict[str, bool] | None = None,
    db=None,
) -> dict:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        current = await get_actor_config(actor, db=db)
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        min_minutes = current["min_interval_minutes"] if min_interval_minutes is None else _clamp_minutes(min_interval_minutes, 5)
        max_minutes = current["max_interval_minutes"] if max_interval_minutes is None else _clamp_minutes(max_interval_minutes, 1440)
        next_relationship_started_on = (
            current["relationship_started_on"]
            if relationship_started_on is None
            else str(relationship_started_on)
        )
        if max_minutes < min_minutes:
            min_minutes, max_minutes = max_minutes, min_minutes
        next_actions = dict(current["actions"])
        if isinstance(actions, dict):
            for key in ACTION_IDS:
                if key in actions:
                    next_actions[key] = bool(actions[key])
        now = time.time()
        timer_started_at = current.get("timer_started_at")
        next_wake_at = current.get("next_wake_at")
        intervals_changed = min_interval_minutes is not None or max_interval_minutes is not None
        if not next_enabled:
            timer_started_at = None
            next_wake_at = None
        elif not current["enabled"] or intervals_changed:
            timer_started_at = now
            next_wake_at = now + random.randint(min_minutes, max_minutes) * 60
        await db.execute(
            "UPDATE autonomy_actor_configs SET enabled=?,min_interval_minutes=?,max_interval_minutes=?,relationship_started_on=?,actions_json=?,"
            "timer_started_at=?,next_wake_at=?,updated_at=? WHERE actor_id=?",
            (
                int(next_enabled), min_minutes, max_minutes, next_relationship_started_on,
                json.dumps(next_actions, ensure_ascii=False),
                timer_started_at, next_wake_at, now, actor,
            ),
        )
        if enabled is False:
            await db.execute(
                "UPDATE autonomy_state_packets SET status='cancelled_by_disable',wake_at=NULL,updated_at=? "
                "WHERE actor_id=? AND status IN ('active','running')",
                (now, actor),
            )
        await db.commit()
        return await get_actor_config(actor, db=db)
    finally:
        if owns_db:
            await db.close()


async def schedule_actor_wake(
    actor: str,
    *,
    anchor_at: float | None = None,
    delay_minutes: int | None = None,
    db=None,
) -> dict:
    """Start one actor's next countdown without touching the other actor."""
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cfg = await get_actor_config(actor, db=db)
        if not cfg["enabled"]:
            await db.execute(
                "UPDATE autonomy_actor_configs SET timer_started_at=NULL,next_wake_at=NULL,updated_at=? WHERE actor_id=?",
                (time.time(), actor),
            )
            await db.commit()
            return await get_actor_config(actor, db=db)
        anchor = float(time.time() if anchor_at is None else anchor_at)
        if delay_minutes is None:
            delay = random.randint(cfg["min_interval_minutes"], cfg["max_interval_minutes"])
        else:
            delay = max(cfg["min_interval_minutes"], min(cfg["max_interval_minutes"], int(delay_minutes)))
        await db.execute(
            "UPDATE autonomy_actor_configs SET timer_started_at=?,next_wake_at=?,updated_at=? WHERE actor_id=?",
            (anchor, anchor + delay * 60, time.time(), actor),
        )
        await db.commit()
        return await get_actor_config(actor, db=db)
    finally:
        if owns_db:
            await db.close()


async def refresh_actor_wake_for_user(
    actor: str,
    *,
    latest_user_at: float,
    delay_minutes: int | None = None,
    db=None,
) -> bool:
    """Restart an actor's countdown once for each newer user message."""
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cfg = await get_actor_config(actor, db=db)
        if not cfg["enabled"] or not latest_user_at:
            return False
        if cfg.get("timer_started_at") is not None and latest_user_at <= cfg["timer_started_at"]:
            return False
        await schedule_actor_wake(
            actor,
            anchor_at=latest_user_at,
            delay_minutes=delay_minutes,
            db=db,
        )
        return True
    finally:
        if owns_db:
            await db.close()


async def claim_due_wake(actor: str, *, now: float | None = None, db=None) -> dict | None:
    """Atomically claim one due actor countdown."""
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        timestamp = float(time.time() if now is None else now)
        cur = await db.execute(
            "SELECT * FROM autonomy_actor_configs WHERE actor_id=? AND enabled=1 "
            "AND next_wake_at IS NOT NULL AND next_wake_at<=?",
            (actor, timestamp),
        )
        row = await cur.fetchone()
        if not row:
            return None
        claimed = _config_from_row(row)
        cur = await db.execute(
            "UPDATE autonomy_actor_configs SET next_wake_at=NULL,updated_at=? "
            "WHERE actor_id=? AND enabled=1 AND next_wake_at IS NOT NULL AND next_wake_at<=?",
            (timestamp, actor, timestamp),
        )
        await db.commit()
        return claimed if cur.rowcount else None
    finally:
        if owns_db:
            await db.close()


def consume_state_block(text: str) -> tuple[str, dict | None, str]:
    source = str(text or "")
    matches = list(_STATE_BLOCK_RE.finditer(source))
    if matches:
        raw = matches[-1].group(1).strip()
        clean = _STATE_BLOCK_RE.sub("", source).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return clean, None, f"invalid autonomy state: {exc.msg}"
        if not isinstance(payload, dict):
            return clean, None, "autonomy state must be an object"
        return clean, payload, ""

    open_matches = list(_STATE_OPEN_RE.finditer(source))
    if open_matches:
        opening = open_matches[-1]
        clean = source[:opening.start()].strip()
        raw = source[opening.end():].lstrip()
        try:
            payload, _end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError as exc:
            return clean, None, f"invalid unclosed autonomy state: {exc.msg}"
        if not isinstance(payload, dict):
            return clean, None, "unclosed autonomy state must be an object"
        return clean, payload, "recovered unclosed autonomy state"

    marker_matches = list(_STATE_MARKER_RE.finditer(source))
    if marker_matches:
        return source[:marker_matches[-1].start()].strip(), None, "invalid unclosed autonomy state tag"
    return source, None, "missing autonomy state"


def _short_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_state_payload(payload: dict, min_minutes: int, max_minutes: int) -> tuple[dict, str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        payload = {}
        errors.append("invalid payload")
    facts = [_short_text(item, 160) for item in payload.get("facts", []) if _short_text(item, 160)] if isinstance(payload.get("facts"), list) else []
    guesses = [_short_text(item, 160) for item in payload.get("guesses", []) if _short_text(item, 160)] if isinstance(payload.get("guesses"), list) else []
    raw_intentions = payload.get("intentions") if isinstance(payload.get("intentions"), list) else []
    intentions = []
    for item in raw_intentions[:3]:
        if isinstance(item, str):
            kind = "goal"
            text = _short_text(item, 180)
        elif isinstance(item, dict):
            kind = str(item.get("type") or "goal").strip().lower()
            text = _short_text(item.get("text"), 180)
        else:
            continue
        if text:
            if kind not in {"goal", "promise", "followup", "observe"}:
                kind = "goal"
            intentions.append({"type": kind, "text": text})
    if len(facts) > 2 or len(guesses) > 2 or len(raw_intentions) > 3:
        errors.append("trimmed live items")
    next_data = payload.get("next") if isinstance(payload.get("next"), dict) else {}
    low = _clamp_minutes(min_minutes, 5)
    high = _clamp_minutes(max_minutes, 1440)
    if high < low:
        low, high = high, low
    requested = _clamp_minutes(next_data.get("after_minutes"), max(low, min(high, 120)))
    bounded = max(low, min(high, requested))
    if bounded != requested or next_data.get("after_minutes") != bounded:
        errors.append("bounded wake time")
    normalized = {
        "state": _short_text(payload.get("state"), 240),
        "facts": facts[:2],
        "guesses": guesses[:2],
        "intentions": intentions,
        "next": {
            "after_minutes": bounded,
            "purpose": _short_text(next_data.get("purpose"), 180),
            "check": _short_text(next_data.get("check"), 180),
        },
    }
    while len(json.dumps(normalized, ensure_ascii=False)) > MAX_STATE_CHARS and normalized["intentions"]:
        normalized["intentions"].pop()
        errors.append("trimmed state length")
    return normalized, "; ".join(dict.fromkeys(errors))


def _packet_from_row(row) -> dict:
    state = json.loads(row["state_json"] or "{}")
    return {
        "id": row["id"],
        "actor": row["actor_id"],
        "previous_id": row["previous_id"] or "",
        "source": row["source"],
        "state": state,
        "wake_at": float(row["wake_at"]) if row["wake_at"] is not None else None,
        "status": row["status"],
        "last_action": row["last_action"] or "",
        "error": row["error"] or "",
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


async def get_current_packet(actor: str, db=None) -> dict | None:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cur = await db.execute(
            "SELECT * FROM autonomy_state_packets WHERE actor_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (actor,),
        )
        row = await cur.fetchone()
        return _packet_from_row(row) if row else None
    finally:
        if owns_db:
            await db.close()


async def list_packets(actor: str, limit: int = 50, db=None) -> list[dict]:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cur = await db.execute(
            "SELECT * FROM autonomy_state_packets WHERE actor_id=? ORDER BY created_at DESC LIMIT ?",
            (actor, max(1, min(200, int(limit)))),
        )
        return [_packet_from_row(row) for row in await cur.fetchall()]
    finally:
        if owns_db:
            await db.close()


async def record_persona_state(
    actor: str,
    payload: dict,
    source: str,
    created_at: float,
    error: str = "",
    *,
    last_action: str = "",
    db=None,
) -> dict:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        cfg = await get_actor_config(actor, db=db)
        normalized, validation_error = normalize_state_payload(
            payload, cfg["min_interval_minutes"], cfg["max_interval_minutes"]
        )
        previous = await get_current_packet(actor, db=db)
        now = float(created_at or time.time())
        packet_id = f"asp_{actor}_{uuid.uuid4().hex}"
        wake_at = now + normalized["next"]["after_minutes"] * 60 if cfg["enabled"] else None
        status = "active" if cfg["enabled"] else "cancelled_by_disable"
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE autonomy_state_packets SET status='superseded',wake_at=NULL,updated_at=? WHERE actor_id=? AND status='active'",
            (now, actor),
        )
        await db.execute(
            "INSERT INTO autonomy_state_packets (id,actor_id,previous_id,source,state_json,wake_at,status,last_action,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                packet_id, actor, previous["id"] if previous else "", _short_text(source, 80),
                json.dumps(normalized, ensure_ascii=False), wake_at, status, _short_text(last_action, 240),
                "; ".join(filter(None, [error, validation_error])), now, now,
            ),
        )
        await db.commit()
        return await get_packet(packet_id, db=db)
    finally:
        if owns_db:
            await db.close()


async def get_packet(packet_id: str, db=None) -> dict | None:
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute("SELECT * FROM autonomy_state_packets WHERE id=?", (packet_id,))
        row = await cur.fetchone()
        return _packet_from_row(row) if row else None
    finally:
        if owns_db:
            await db.close()


async def expire_overdue_packets(db=None, *, now: float | None = None) -> int:
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
    try:
        timestamp = float(now or time.time())
        cur = await db.execute(
            "UPDATE autonomy_state_packets SET status='missed_during_downtime',wake_at=NULL,updated_at=? "
            "WHERE status='active' AND wake_at IS NOT NULL AND wake_at<=?",
            (timestamp, timestamp),
        )
        changed = int(cur.rowcount or 0)
        cur = await db.execute(
            "UPDATE autonomy_state_packets SET status='abandoned',wake_at=NULL,updated_at=?,"
            "error=CASE WHEN error='' THEN 'interrupted by server shutdown' ELSE error END "
            "WHERE status='running'",
            (timestamp,),
        )
        changed += int(cur.rowcount or 0)
        await db.commit()
        return changed
    finally:
        if owns_db:
            await db.close()


async def claim_due_packet(actor: str, *, now: float | None = None, db=None) -> dict | None:
    actor = normalize_actor(actor)
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
    try:
        await ensure_autonomy_tables(db)
        timestamp = float(now or time.time())
        cur = await db.execute(
            "SELECT p.id FROM autonomy_state_packets p "
            "JOIN autonomy_actor_configs c ON c.actor_id=p.actor_id "
            "WHERE p.actor_id=? AND p.status='active' AND p.wake_at IS NOT NULL "
            "AND p.wake_at<=? AND c.enabled=1 ORDER BY p.created_at DESC LIMIT 1",
            (actor, timestamp),
        )
        row = await cur.fetchone()
        if not row:
            return None
        packet_id = row["id"] if hasattr(row, "keys") else row[0]
        cur = await db.execute(
            "UPDATE autonomy_state_packets SET status='running',wake_at=NULL,updated_at=? "
            "WHERE id=? AND status='active'",
            (timestamp, packet_id),
        )
        await db.commit()
        if not cur.rowcount:
            return None
        return await get_packet(packet_id, db=db)
    finally:
        if owns_db:
            await db.close()


async def finish_claimed_packet(packet_id: str, status: str, *, error: str = "", db=None) -> None:
    if status not in {"completed", "abandoned", "failed"}:
        raise ValueError("invalid autonomy completion status")
    owns_db = db is None
    if owns_db:
        db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE autonomy_state_packets SET status=?,error=CASE WHEN ?='' THEN error ELSE ? END,updated_at=? "
            "WHERE id=? AND status='running'",
            (status, error, error, time.time(), packet_id),
        )
        await db.commit()
    finally:
        if owns_db:
            await db.close()


async def autonomy_prompt_text(actor: str) -> str:
    normalize_actor(actor)
    return ""


def strip_autonomy_state(text: str) -> str:
    clean, _payload, _error = consume_state_block(text)
    return clean


async def inject_autonomy_ability(messages: list[dict], actor: str) -> list[dict]:
    text = await autonomy_prompt_text(actor)
    if not text:
        return messages
    insert_at = len(messages)
    if messages and messages[-1].get("role") == "user":
        insert_at -= 1
    messages.insert(insert_at, {"role": "system", "content": text})
    return messages


def _event_actor(event: dict) -> str | None:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event.get("type") == "msg_created" and str(data.get("role") or "").lower() == "assistant":
        return "aion"
    if event.get("type") == "chatroom_msg_created":
        sender = str(data.get("sender") or "").lower()
        return sender if sender in ACTOR_IDS else None
    return None


async def _persist_clean_event_content(event: dict, content: str) -> None:
    table = "messages" if event.get("type") == "msg_created" else "chatroom_messages"
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    message_id = str(data.get("id") or "")
    if not message_id:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE {table} SET content=? WHERE id=?", (content, message_id))
            await db.commit()
    except aiosqlite.OperationalError:
        pass


async def process_persona_message_event(event: dict) -> tuple[dict, bool]:
    actor = _event_actor(event)
    if actor is None:
        return event, False
    updated = copy.deepcopy(event)
    data = updated["data"]
    data["content"] = strip_autonomy_state(str(data.get("content") or ""))
    return updated, False


async def autonomy_status_payload() -> dict:
    from chatroom import get_chatroom_names

    _user_name, ai_name, connor_name = get_chatroom_names()
    names = {"aion": ai_name, "connor": connor_name}
    roles = []
    now = time.time()
    for actor in ACTOR_IDS:
        cfg = await get_actor_config(actor)
        wake_at = cfg.get("next_wake_at")
        roles.append({
            "actor": actor,
            "name": names[actor],
            "config": cfg,
            "current": None,
            "latest": None,
            "next_wake_at": wake_at,
            "remaining_minutes": max(0, int((wake_at - now + 59) // 60)) if wake_at else None,
        })
    return {"roles": roles}
