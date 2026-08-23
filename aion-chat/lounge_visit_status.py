"""Persist short-lived, non-model-context lounge visit status lines."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from lounge_visit_reporting import _clean


Channel = Literal["private", "chatroom"]


@dataclass(frozen=True)
class VisitStatusHandle:
    channel: Channel
    scope_id: str
    message_id: str
    status_id: str


@dataclass(frozen=True)
class StatusMutation:
    action: Literal["updated", "deleted"]
    channel: Channel
    scope_id: str
    message_id: str
    message: dict | None = None


def _names(actor_name: str, friend_name: str) -> tuple[str, str]:
    return _clean(actor_name, 80) or "AI", _clean(friend_name, 80) or "朋友"


def _attachments(status_id: str, actor_name: str, friend_name: str, after_msg_id: str) -> list[dict]:
    items = [
        {
            "type": "lounge_visit_status",
            "status_id": status_id,
            "state": "active",
            "actor_name": actor_name,
            "friend_name": friend_name,
        }
    ]
    if after_msg_id:
        items.append({"type": "system_notice_order", "after_msg_id": after_msg_id})
    return items


async def create_private_status(
    db,
    conv_id: str,
    actor_name: str,
    friend_name: str,
    after_msg_id: str,
) -> tuple[VisitStatusHandle, dict]:
    actor, friend = _names(actor_name, friend_name)
    status_id = uuid.uuid4().hex
    message_id = f"msg_{time.time_ns()}_lounge_status"
    created_at = time.time()
    attachments = _attachments(status_id, actor, friend, after_msg_id)
    content = f"{actor} 正在前往拜访 {friend}…"
    await db.execute(
        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
        (
            message_id,
            conv_id,
            "system",
            content,
            created_at,
            json.dumps(attachments, ensure_ascii=False),
        ),
    )
    await db.commit()
    handle = VisitStatusHandle("private", conv_id, message_id, status_id)
    return handle, {
        "id": message_id,
        "conv_id": conv_id,
        "role": "system",
        "content": content,
        "created_at": created_at,
        "attachments": attachments,
    }


async def create_chatroom_status(
    db,
    room_id: str,
    actor_name: str,
    friend_name: str,
    after_msg_id: str = "",
) -> tuple[VisitStatusHandle, dict]:
    actor, friend = _names(actor_name, friend_name)
    status_id = uuid.uuid4().hex
    message_id = f"cm_{time.time_ns()}_lounge_status"
    created_at = time.time()
    attachments = _attachments(status_id, actor, friend, after_msg_id)
    content = f"{actor} 正在前往拜访 {friend}…"
    await db.execute(
        "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
        (
            message_id,
            room_id,
            "system",
            content,
            created_at,
            json.dumps(attachments, ensure_ascii=False),
        ),
    )
    await db.commit()
    handle = VisitStatusHandle("chatroom", room_id, message_id, status_id)
    return handle, {
        "id": message_id,
        "room_id": room_id,
        "sender": "system",
        "content": content,
        "created_at": created_at,
        "attachments": attachments,
    }


def _storage(channel: Channel) -> tuple[str, str, str]:
    if channel == "private":
        return "messages", "conv_id", "role"
    return "chatroom_messages", "room_id", "sender"


def _parse_attachments(raw: object) -> list[dict]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _status_marker(attachments: list[dict], status_id: str) -> dict | None:
    return next(
        (
            item
            for item in attachments
            if item.get("type") == "lounge_visit_status"
            and item.get("status_id") == status_id
        ),
        None,
    )


async def remove_status(db, handle: VisitStatusHandle) -> bool:
    table, scope_column, sender_column = _storage(handle.channel)
    cursor = await db.execute(
        f"SELECT attachments FROM {table} WHERE id=? AND {scope_column}=? AND {sender_column}='system'",
        (handle.message_id, handle.scope_id),
    )
    row = await cursor.fetchone()
    if not row or _status_marker(_parse_attachments(row[0]), handle.status_id) is None:
        return False
    await db.execute(
        f"DELETE FROM {table} WHERE id=? AND {scope_column}=?",
        (handle.message_id, handle.scope_id),
    )
    await db.commit()
    return True


async def downgrade_status(db, handle: VisitStatusHandle) -> dict | None:
    table, scope_column, sender_column = _storage(handle.channel)
    cursor = await db.execute(
        f"SELECT content, created_at, attachments FROM {table} WHERE id=? AND {scope_column}=? AND {sender_column}='system'",
        (handle.message_id, handle.scope_id),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    attachments = _parse_attachments(row[2])
    marker = _status_marker(attachments, handle.status_id)
    if marker is None or marker.get("state") != "active":
        return None
    marker["state"] = "interrupted"
    actor_name = _clean(marker.get("actor_name"), 80) or "AI"
    content = f"{actor_name} 的这次拜访中断了，可在串门记录中查看。"
    await db.execute(
        f"UPDATE {table} SET content=?, attachments=? WHERE id=? AND {scope_column}=?",
        (
            content,
            json.dumps(attachments, ensure_ascii=False),
            handle.message_id,
            handle.scope_id,
        ),
    )
    await db.commit()
    message = {
        "id": handle.message_id,
        "content": content,
        "created_at": row[1],
        "attachments": attachments,
    }
    if handle.channel == "private":
        message.update({"conv_id": handle.scope_id, "role": "system"})
    else:
        message.update({"room_id": handle.scope_id, "sender": "system"})
    return message


async def _report_status_ids(db, table: str, scope_column: str, scope_id: str) -> set[str]:
    cursor = await db.execute(
        f"SELECT attachments FROM {table} WHERE {scope_column}=? AND attachments LIKE ?",
        (scope_id, '%"lounge_visit_report"%'),
    )
    found: set[str] = set()
    for row in await cursor.fetchall():
        for item in _parse_attachments(row[0]):
            if item.get("type") == "lounge_visit_report" and item.get("status_id"):
                found.add(str(item["status_id"]))
    return found


async def recover_stale_statuses(db) -> list[StatusMutation]:
    mutations: list[StatusMutation] = []
    for channel in ("private", "chatroom"):
        table, scope_column, sender_column = _storage(channel)
        cursor = await db.execute(
            f"SELECT id, {scope_column}, attachments FROM {table} WHERE {sender_column}='system' AND attachments LIKE ?",
            ('%"lounge_visit_status"%',),
        )
        for message_id, scope_id, raw_attachments in await cursor.fetchall():
            attachments = _parse_attachments(raw_attachments)
            marker = next(
                (item for item in attachments if item.get("type") == "lounge_visit_status"),
                None,
            )
            if marker is None or marker.get("state") != "active":
                continue
            status_id = str(marker.get("status_id") or "")
            handle = VisitStatusHandle(channel, scope_id, message_id, status_id)
            report_ids = await _report_status_ids(db, table, scope_column, scope_id)
            if status_id and status_id in report_ids:
                if await remove_status(db, handle):
                    mutations.append(
                        StatusMutation("deleted", channel, scope_id, message_id)
                    )
                continue
            message = await downgrade_status(db, handle)
            if message is not None:
                mutations.append(
                    StatusMutation("updated", channel, scope_id, message_id, message)
                )
    return mutations
