import json
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query

from chatroom import get_chatroom_names
from config import load_worldbook
from database import get_db

router = APIRouter(prefix="/api/search", tags=["search"])


def _like_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean_content(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _attachments_count(raw: Optional[str]) -> int:
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


@router.get("/messages")
async def search_messages(q: str = Query(..., min_length=1, max_length=80), limit: int = Query(60, ge=1, le=120)):
    keyword = q.strip()
    if not keyword:
        return {"items": []}

    wb = load_worldbook()
    user_name, aion_name, connor_name = get_chatroom_names()
    user_name = user_name or wb.get("user_name") or "你"
    aion_name = aion_name or wb.get("ai_name") or "AI"
    connor_name = connor_name or "Connor"
    name_map = {
        "user": user_name,
        "assistant": aion_name,
        "aion": aion_name,
        "connor": connor_name,
        "system": "系统",
    }

    like = f"%{_like_escape(keyword)}%"
    main_limit = limit
    room_limit = limit

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        main_cur = await db.execute(
            "SELECT m.id, m.conv_id AS target_id, m.role AS speaker_key, m.content, m.attachments, "
            "m.created_at, c.title AS target_title "
            "FROM messages m "
            "LEFT JOIN conversations c ON c.id = m.conv_id "
            "WHERE COALESCE(m.content,'') LIKE ? ESCAPE '\\' "
            "ORDER BY m.created_at DESC LIMIT ?",
            (like, main_limit),
        )
        main_rows = await main_cur.fetchall()

        room_cur = await db.execute(
            "SELECT m.id, m.room_id AS target_id, m.sender AS speaker_key, m.content, m.attachments, "
            "m.created_at, r.title AS target_title, r.type AS room_type "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id "
            "WHERE r.type IN ('group','connor_1v1') AND COALESCE(m.content,'') LIKE ? ESCAPE '\\' "
            "ORDER BY m.created_at DESC LIMIT ?",
            (like, room_limit),
        )
        room_rows = await room_cur.fetchall()

    items = []
    for row in main_rows:
        items.append({
            "source": "aion_private",
            "source_label": f"{aion_name}私聊",
            "id": row["id"],
            "target_id": row["target_id"],
            "target_title": row["target_title"] or f"{aion_name}私聊",
            "speaker_key": row["speaker_key"],
            "speaker_name": name_map.get(row["speaker_key"], row["speaker_key"]),
            "content": _clean_content(row["content"]),
            "attachments_count": _attachments_count(row["attachments"]),
            "created_at": row["created_at"],
            "url": f"/chat?conv={row['target_id']}&msg={row['id']}",
        })

    for row in room_rows:
        room_type = row["room_type"] or "group"
        is_connor_private = room_type == "connor_1v1"
        source = "connor_private" if is_connor_private else "group"
        source_label = f"{connor_name}私聊" if is_connor_private else "群聊"
        items.append({
            "source": source,
            "source_label": source_label,
            "id": row["id"],
            "target_id": row["target_id"],
            "target_title": row["target_title"] or source_label,
            "speaker_key": row["speaker_key"],
            "speaker_name": name_map.get(row["speaker_key"], row["speaker_key"]),
            "content": _clean_content(row["content"]),
            "attachments_count": _attachments_count(row["attachments"]),
            "created_at": row["created_at"],
            "url": f"/chatroom?room={row['target_id']}&msg={row['id']}",
        })

    items.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return {
        "items": items[:limit],
        "query": keyword,
        "names": {"user": user_name, "aion": aion_name, "connor": connor_name},
    }
