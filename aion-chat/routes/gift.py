"""
礼物系统 API 路由
"""

import asyncio
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from PIL import Image

from config import DATA_DIR, UPLOADS_DIR, ALBUM_IMAGES_DIR
from database import get_db
from gift import get_pending_gifts, receive_gift, list_gifts, delete_gift

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


@router.post("/test")
async def api_test():
    """测试送礼：取最近5条记忆，强制触发完整送礼流程"""
    import aiosqlite
    from database import get_db
    from config import load_worldbook, DEFAULT_MODEL

    # 获取最近5条记忆作为摘要
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT content FROM memories ORDER BY created_at DESC LIMIT 5"
        )
        rows = await cur.fetchall()
    if not rows:
        return {"ok": False, "message": "记忆库为空，无法测试"}
    all_summaries = [r["content"] for r in rows]

    # 获取最近对话的模型和 conv_id
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT c.id, c.model FROM conversations c ORDER BY c.updated_at DESC LIMIT 1"
        )
        conv_row = await cur.fetchone()
    if not conv_row:
        return {"ok": False, "message": "没有对话，无法测试"}

    model_key = conv_row["model"] or DEFAULT_MODEL
    conv_id = conv_row["id"]

    # 获取最近聊天上下文
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages "
            "WHERE conv_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT 20",
            (conv_id,)
        )
        recent_rows = list(reversed(await cur.fetchall()))
    context_msgs = [{"role": r["role"], "content": r["content"][:300]} for r in recent_rows]

    # 构建人设
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")
    persona_block = ""
    if ai_persona:
        persona_block += f"[{ai_name}的人设]\n{ai_persona}\n\n"
    if user_persona:
        persona_block += f"[{user_name}的人设]\n{user_persona}\n\n"

    # 调用送礼流程
    from gift import judge_and_send_gift
    await judge_and_send_gift(
        all_summaries, context_msgs, persona_block,
        ai_name, user_name, model_key, conv_id,
    )
    return {"ok": True, "message": "测试送礼流程已触发，请等待AI判断和生图..."}
