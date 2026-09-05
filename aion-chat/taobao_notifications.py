"""Shopping receipts delivered to the actor's existing chat window."""
import json
import re
import time

from database import get_db
from chatroom import get_chatroom_names
from sync_events import append_sync_event, broadcast_synced
from ws import manager


async def notify_shopping_trip(actor, trip_id, items):
    if not items:
        return None
    if actor not in {"aion", "connor"} or not re.fullmatch(r"[A-Za-z0-9_-]+", trip_id):
        raise ValueError("无效的逛街通知")
    _, first, second = get_chatroom_names()
    name = (first if actor == "aion" else second) or "AI"
    content = f"{name} 逛淘宝，往心愿袋里塞了 {len(items)} 件商品"
    card = {"type": "taobao_trip", "trip_id": trip_id, "actor": actor, "count": len(items),
            "products": [{k: p.get(k, "") for k in ("item_id", "title", "image")} for p in items[:3]]}
    now, message_id = time.time(), "taobao_" + trip_id
    # Same transaction for receipt and sync event. No dialogue or TTS entry point.
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        for table in ("messages", "chatroom_messages"):
            if await (await db.execute(f"SELECT 1 FROM {table} WHERE id=?", (message_id,))).fetchone():
                return None
        target = manager.get_aion_last_active() if actor == "aion" else manager.get_connor_last_active()
        room_id = target.removeprefix("chatroom:") if target and (actor == "connor" or target.startswith("chatroom:")) else None
        room = None
        if room_id:
            types = ("group", "group") if actor == "aion" else ("group", "connor_1v1")
            room = await (await db.execute("SELECT id FROM chatroom_rooms WHERE id=? AND type IN (?,?)", (room_id, *types))).fetchone()
        if not room and actor == "connor":
            room = await (await db.execute("SELECT id FROM chatroom_rooms WHERE type IN ('group','connor_1v1') ORDER BY updated_at DESC LIMIT 1")).fetchone()
        if room:
            table, scope, role, target_id, event_type = "chatroom_messages", "room_id", "sender", room[0], "chatroom_msg_created"
            parent_table = "chatroom_rooms"
        else:
            if actor != "aion":
                return None
            # Renames/background replies also change updated_at, but are not user activity.
            conv = await (await db.execute("""SELECT c.id FROM conversations c
                JOIN messages m ON m.conv_id=c.id WHERE m.role='user'
                ORDER BY m.created_at DESC LIMIT 1""")).fetchone()
            if not conv:
                conv = await (await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")).fetchone()
            if not conv:
                return None
            table, scope, role, target_id, event_type = "messages", "conv_id", "role", conv[0], "msg_created"
            parent_table = "conversations"
        message = {"id": message_id, scope: target_id, role: "system", "content": content,
                   "created_at": now, "attachments": [card]}
        await db.execute(f"INSERT INTO {table}(id,{scope},{role},content,created_at,attachments) VALUES(?,?,?,?,?,?)",
                         (message_id, target_id, "system", content, now, json.dumps([card], ensure_ascii=False)))
        await db.execute(f"UPDATE {parent_table} SET updated_at=? WHERE id=?", (now, target_id))
        event = {"type": event_type, "data": message}
        seq = await append_sync_event(db, event)
        await db.commit()
    await broadcast_synced(manager, event, seq=seq)
    return message
