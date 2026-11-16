"""
礼物系统 API 路由
"""

import asyncio
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from PIL import Image

from config import DATA_DIR, UPLOADS_DIR, ALBUM_IMAGES_DIR
from database import get_db
from gift import get_pending_gifts, receive_gift, list_gifts, delete_gift, delete_gifts_with_files

router = APIRouter(prefix="/api/gift", tags=["gift"])
THUMBNAIL_DIR = DATA_DIR / "gift_thumbnails"
THUMBNAIL_DIR.mkdir(exist_ok=True)


def _build_thumbnail(source: Path, destination: Path):
    temp = destination.with_suffix(".part.webp")
    with Image.open(source) as image:
        image.thumbnail((480, 480), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(temp, "WEBP", quality=76, method=6)
    temp.replace(destination)


@router.get("/pending")
async def api_pending():
    """查询未领取的礼物"""
    gifts = await get_pending_gifts()
    return {"ok": True, "gifts": gifts}


@router.post("/{gift_id}/receive")
async def api_receive(gift_id: str):
    """标记礼物为已领取"""
    ok = await receive_gift(gift_id)
    return {"ok": ok}


@router.get("/list")
async def api_list():
    """查询所有已领取的礼物（陈列馆）"""
    gifts = await list_gifts()
    return {"ok": True, "gifts": gifts}


@router.get("/thumbnail/{gift_id}")
async def api_thumbnail(gift_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT image_path FROM gifts WHERE id=?", (gift_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="gift not found")

    relative_path = row["image_path"]
    image_root = ALBUM_IMAGES_DIR if relative_path.startswith("album/") else UPLOADS_DIR
    source = (image_root / (relative_path[len("album/"):] if relative_path.startswith("album/") else relative_path)).resolve()
    if image_root.resolve() not in source.parents or not source.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    destination = THUMBNAIL_DIR / f"{gift_id}.webp"
    if not destination.is_file() or destination.stat().st_mtime_ns < source.stat().st_mtime_ns:
        await asyncio.to_thread(_build_thumbnail, source, destination)
    return FileResponse(
        destination,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.delete("/{gift_id}")
async def api_delete(gift_id: str):
    """删除礼物"""
    ok = await delete_gift(gift_id)
    if ok:
        try:
            (THUMBNAIL_DIR / f"{gift_id}.webp").unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": ok}


@router.post("/batch-delete")
async def api_batch_delete(body: dict):
    """批量删除礼物，连同原图和缩略图文件一起删除"""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids required")
    result = await delete_gifts_with_files(ids)
    freed = result["freed_bytes"]
    for gift_id in result["deleted_ids"]:
        thumb = THUMBNAIL_DIR / f"{Path(gift_id).name}.webp"
        try:
            size = thumb.stat().st_size
            thumb.unlink()
            freed += size
        except OSError:
            pass
    return {"ok": True, "deleted_ids": result["deleted_ids"], "freed_bytes": freed}


async def _recent_gift_context(sender: str, limit: int = 30) -> tuple[str, int]:
    """最近的私聊 + 群聊消息，按时间合并后取最新 limit 条。"""
    from chatroom import get_chatroom_names
    user_name, ai_name, connor_name = get_chatroom_names()
    names = {"user": user_name, "aion": ai_name, "connor": connor_name}
    rows = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if sender == "connor":
            cur = await db.execute(
                "SELECT m.sender, m.content, m.created_at FROM chatroom_messages m "
                "JOIN chatroom_rooms r ON r.id = m.room_id "
                "WHERE r.type = 'connor_1v1' AND m.sender != 'system' "
                "ORDER BY m.created_at DESC LIMIT ?", (limit,))
            rows += [("私聊", names.get(r["sender"], r["sender"]), r["content"], r["created_at"])
                     for r in await cur.fetchall()]
        else:
            cur = await db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE role IN ('user','assistant') ORDER BY created_at DESC LIMIT ?", (limit,))
            rows += [("私聊", user_name if r["role"] == "user" else ai_name, r["content"], r["created_at"])
                     for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT m.sender, m.content, m.created_at FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id "
            "WHERE r.type = 'group' AND m.sender != 'system' "
            "ORDER BY m.created_at DESC LIMIT ?", (limit,))
        rows += [("群聊", names.get(r["sender"], r["sender"]), r["content"], r["created_at"])
                 for r in await cur.fetchall()]
    rows = [r for r in rows if (r[2] or "").strip()]
    rows = sorted(rows, key=lambda r: r[3])[-limit:]
    lines = [
        f"[{time.strftime('%m-%d %H:%M', time.localtime(ts))}][{source}] {name}: {content.strip()[:300]}"
        for source, name, content, ts in rows
    ]
    return "\n".join(lines), len(lines)


@router.post("/test")
async def api_test(sender: str = "aion"):
    """测试送礼：最新5条记忆 + 最近的私聊/群聊消息，跳过“要不要送”的判断，必定生图送礼"""
    from config import load_worldbook, DEFAULT_MODEL
    from chatroom import get_chatroom_names, _read_connor_persona

    if sender not in ("aion", "connor"):
        raise HTTPException(status_code=400, detail="unknown sender")

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT content FROM memories ORDER BY created_at DESC LIMIT 5"
        )
        all_summaries = [r["content"] for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT c.id, c.model FROM conversations c ORDER BY c.updated_at DESC LIMIT 1"
        )
        conv_row = await cur.fetchone()
    if not all_summaries:
        return {"ok": False, "message": "记忆库为空，无法测试"}

    context_text, context_count = await _recent_gift_context(sender)
    if not context_count:
        return {"ok": False, "message": "没有聊天记录，无法测试"}

    wb = load_worldbook()
    user_name, ai_name, connor_name = get_chatroom_names()
    user_persona = wb.get("user_persona", "")
    persona_block = ""
    if sender == "connor":
        connor_persona = _read_connor_persona()
        if connor_persona:
            persona_block += f"[{connor_name}的人设]\n{connor_persona}\n\n"
        if user_persona:
            persona_block += f"[{user_name}的信息]\n{user_persona}\n\n"
    else:
        if wb.get("ai_persona"):
            persona_block += f"[{ai_name}的人设]\n{wb['ai_persona']}\n\n"
        if user_persona:
            persona_block += f"[{user_name}的人设]\n{user_persona}\n\n"

    from gift import judge_and_send_gift
    status = await judge_and_send_gift(
        all_summaries, [], persona_block,
        connor_name if sender == "connor" else ai_name, user_name,
        (conv_row["model"] if conv_row else None) or DEFAULT_MODEL,
        conv_row["id"] if conv_row else "",
        sender=sender, force=True, context_text=context_text,
    )
    messages = {
        "sent": "礼物已送达，去首页拆开吧",
        "no_response": "AI 没有回应，请稍后再试",
        "parse_failed": "AI 返回的内容无法解析，请再试一次",
        "invalid": "AI 没有给出生图提示词或赠言，请再试一次",
        "image_failed": "生图失败，请检查设置里的生图模型配置",
    }
    return {"ok": status == "sent", "message": messages.get(status, "送礼未完成")}
