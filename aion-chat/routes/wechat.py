import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from config import DEFAULT_MODEL, SETTINGS
from database import get_db
from routes.files import export_conversation
from wechat_bridge import build_wechat_user_content, get_recorded_wechat_route
from ws import manager


router = APIRouter(prefix="/api/wechat", tags=["wechat"])


class WeChatInbound(BaseModel):
    content: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    conv_id: Optional[str] = None
    room_id: Optional[str] = None
    auto_reply: bool = True
    context_limit: int = 30
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    mark_channel: bool = True


def _normalize_source_type(source_type: str) -> str:
    value = (source_type or "").strip().lower()
    if value in ("aion_private", "private", "conversation", "conv"):
        return "aion_private"
    if value in ("chatroom", "room", "group", "connor_1v1"):
        return "chatroom"
    return value


def _resolve_route(body: WeChatInbound) -> tuple[str, str]:
    if body.conv_id:
        return "aion_private", body.conv_id.strip()
    if body.room_id:
        return "chatroom", body.room_id.strip()

    route = get_recorded_wechat_route()
    source_type = body.source_type or route.get("source_type") or ""
    source_id = body.source_id or route.get("source_id") or ""
    return _normalize_source_type(source_type), (source_id or "").strip()


def _check_inbound_token(authorization: str | None) -> None:
    expected = str(SETTINGS.get("wechat_bridge_inbound_token") or "").strip()
    if not expected:
        return
    supplied = (authorization or "").strip()
    if supplied == expected or supplied == f"Bearer {expected}":
        return
    raise HTTPException(status_code=401, detail="invalid wechat inbound token")


async def _drain_streaming_response(response) -> None:
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return
    async for _ in iterator:
        pass


async def _save_private_user_message(conv_id: str, content: str) -> dict:
    now = time.time()
    msg_id = f"msg_{time.time_ns()}_wechat_user"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "user", content, now, "[]"),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()
    msg = {
        "id": msg_id,
        "conv_id": conv_id,
        "role": "user",
        "content": content,
        "created_at": now,
        "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": msg})
    await export_conversation(conv_id)
    return msg


@router.post("/inbound")
async def receive_wechat_message(body: WeChatInbound, authorization: str | None = Header(None)):
    _check_inbound_token(authorization)

    raw_content = (body.content or "").strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail="content is required")

    source_type, source_id = _resolve_route(body)
    if not source_type or not source_id:
        raise HTTPException(status_code=400, detail="missing target route; send source_id or trigger an outbound wechat message first")

    content = build_wechat_user_content(raw_content) if body.mark_channel else raw_content

    if source_type == "aion_private":
        if body.auto_reply:
            from routes import chat as chat_routes

            response = await chat_routes.send_message(
                source_id,
                chat_routes.MsgCreate(
                    content=content,
                    context_limit=body.context_limit,
                    client_id="wechat",
                ),
            )
            asyncio.create_task(_drain_streaming_response(response))
            return {"ok": True, "source_type": source_type, "source_id": source_id, "auto_reply": True}

        msg = await _save_private_user_message(source_id, content)
        return {"ok": True, "source_type": source_type, "source_id": source_id, "auto_reply": False, "message": msg}

    if source_type == "chatroom":
        from routes import chatroom as chatroom_routes

        if body.auto_reply:
            response = await chatroom_routes.send_message(
                source_id,
                chatroom_routes.MsgSend(
                    content=content,
                    model=body.model or DEFAULT_MODEL,
                    connor_model=body.connor_model or "Codex",
                ),
            )
            asyncio.create_task(_drain_streaming_response(response))
            return {"ok": True, "source_type": source_type, "source_id": source_id, "auto_reply": True}

        msg = await chatroom_routes._save_msg(source_id, "user", content, attachments=[])
        return {"ok": True, "source_type": source_type, "source_id": source_id, "auto_reply": False, "message": msg}

    raise HTTPException(status_code=400, detail=f"unsupported source_type: {source_type}")
