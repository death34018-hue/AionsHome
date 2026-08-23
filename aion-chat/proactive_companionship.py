"""Short, per-role follow-up timers for natural proactive companionship."""

from __future__ import annotations

import copy
import math
import re
import time
from datetime import datetime

import aiosqlite

from config import DB_PATH, SETTINGS, save_settings


PROACTIVE_TYPE = "proactive"
NO_DECISION = object()
_NEXT_CHAT_RE = re.compile(
    r"[\[［]\s*NEXT_CHAT\s*[:：]\s*([^\]］]+?)\s*[\]］]",
    re.IGNORECASE,
)
_SETTING_KEYS = {
    "aion": "proactive_companionship_aion_enabled",
    "connor": "proactive_companionship_connor_enabled",
}


def _actor_id(actor: str | None) -> str:
    return "connor" if str(actor or "").strip().lower() == "connor" else "aion"


def is_proactive_enabled(actor: str) -> bool:
    return bool(SETTINGS.get(_SETTING_KEYS[_actor_id(actor)], False))


def set_proactive_enabled(actor: str, enabled: bool) -> None:
    SETTINGS[_SETTING_KEYS[_actor_id(actor)]] = bool(enabled)
    save_settings(SETTINGS)


def proactive_ability_text(actor: str) -> str:
    if not is_proactive_enabled(actor):
        return ""
    return (
        "【高优先级要求】请根据当前对话和情境，决定是否需要在短时间无新消息后再次主动联系用户，"
        "在适合延续当前话题时尽量主动安排。"
        "需要时输出 [NEXT_CHAT:x]，x 为 1～60 的整数分钟；"
        "不需要时输出 [NEXT_CHAT:NONE]。每次回复必须输出其中一种决定。"
    )


def inject_proactive_ability(messages: list[dict], actor: str) -> list[dict]:
    text = proactive_ability_text(actor)
    if not text:
        return messages
    insert_at = len(messages)
    if messages and messages[-1].get("role") == "user":
        insert_at -= 1
    messages.insert(insert_at, {"role": "system", "content": text})
    return messages


def extract_next_chat_decision(text: str) -> tuple[str, int | None | object]:
    source = str(text or "")
    decision: int | None | object = NO_DECISION
    for match in _NEXT_CHAT_RE.finditer(source):
        raw = match.group(1).strip()
        if raw.upper() == "NONE":
            decision = None
            continue
        if raw.isdigit():
            minutes = int(raw)
            if 1 <= minutes <= 60:
                decision = minutes
    return _NEXT_CHAT_RE.sub("", source).strip(), decision


def _message_actor(event: dict) -> str | None:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event.get("type") == "msg_created":
        role = str(data.get("role") or "").lower()
        if role == "user":
            return "user"
        if role == "assistant":
            return "aion"
    if event.get("type") == "chatroom_msg_created":
        sender = str(data.get("sender") or "").lower()
        if sender in ("user", "aion", "connor"):
            return sender
    return None


async def process_visible_message_event(event: dict) -> tuple[dict, bool]:
    """Clear old timers, then apply the visible AI message's fresh decision."""
    actor = _message_actor(event)
    if actor is None:
        return event, False

    updated = copy.deepcopy(event)
    data = updated["data"]
    clean_text = str(data.get("content") or "")
    decision: int | None | object = NO_DECISION
    if actor != "user":
        clean_text, decision = extract_next_chat_decision(clean_text)
        data["content"] = clean_text

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if actor == "user":
            cur = await db.execute(
                "DELETE FROM schedules WHERE type=? AND status='active'",
                (PROACTIVE_TYPE,),
            )
        else:
            cur = await db.execute(
                "DELETE FROM schedules WHERE type=? AND status='active' AND origin=?",
                (PROACTIVE_TYPE, actor),
            )
            table = "messages" if updated.get("type") == "msg_created" else "chatroom_messages"
            await db.execute(
                f"UPDATE {table} SET content=? WHERE id=?",
                (clean_text, str(data.get("id") or "")),
            )

            if decision is not NO_DECISION and decision is not None and is_proactive_enabled(actor):
                created_at = float(data.get("created_at") or time.time())
                trigger_at = datetime.fromtimestamp(created_at + decision * 60).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                await db.execute(
                    "INSERT INTO schedules "
                    "(id,type,trigger_at,content,created_at,status,origin,origin_room_id) "
                    "VALUES (?,?,?,?,?,'active',?,'')",
                    (
                        f"pc_{actor}_{time.time_ns()}",
                        PROACTIVE_TYPE,
                        trigger_at,
                        "",
                        created_at,
                        actor,
                    ),
                )
        await db.commit()
        changed = bool(cur.rowcount) or actor != "user"
    return updated, changed


async def claim_proactive_timer(schedule_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM schedules WHERE id=? AND type=? AND status='active'",
            (schedule_id, PROACTIVE_TYPE),
        )
        await db.commit()
        return bool(cur.rowcount)


async def proactive_status_payload() -> dict:
    from chatroom import get_chatroom_names

    _user_name, ai_name, connor_name = get_chatroom_names()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT origin, trigger_at FROM schedules "
            "WHERE type=? AND status='active' ORDER BY trigger_at",
            (PROACTIVE_TYPE,),
        )
        timers = {row["origin"]: row["trigger_at"] for row in await cur.fetchall()}

    now = time.time()
    roles = []
    for actor, name in (("aion", ai_name), ("connor", connor_name)):
        next_at = timers.get(actor, "")
        remaining = None
        if next_at:
            try:
                due_ts = datetime.strptime(next_at, "%Y-%m-%d %H:%M:%S").timestamp()
                remaining = max(0, math.ceil((due_ts - now) / 60))
            except ValueError:
                next_at = ""
        roles.append({
            "actor": actor,
            "name": name,
            "enabled": is_proactive_enabled(actor),
            "active": bool(next_at),
            "next_at": next_at,
            "remaining_minutes": remaining,
        })
    return {"name": "主动陪伴", "roles": roles}


async def proactive_status_event() -> dict:
    return {"type": "proactive_companionship_changed", "data": await proactive_status_payload()}
