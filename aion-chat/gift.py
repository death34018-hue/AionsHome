"""
礼物系统：AI 判断送礼 + 设置中的生图模型生图 + 礼物数据 CRUD
"""

import asyncio, json, time
from datetime import datetime

import aiosqlite

from config import UPLOADS_DIR
from database import get_db
from album import get_album_store
from image_anchors import family_image_prompt, resolve_chat_references
from model_json import extract_json_object
from ws import manager


# ── AI 判断是否送礼 + 生图 + 入库 ─────────────────
async def judge_and_send_gift(
    all_summaries: list[str],
    context_msgs: list[dict],
    persona_block: str,
    ai_name: str,
    user_name: str,
    model_key: str,
    conv_id: str,
    *,
    sender: str = "aion",
    force: bool = False,
    context_text: str = "",
) -> str:
    """
    返回结果：sent / declined / no_response / parse_failed / image_failed / invalid。
    force=True 用于测试按钮：跳过“要不要送”的判断，必定送。
    在记忆总结完成后调用，让 AI 判断是否需要给用户送礼。
    如果是，则生成图片并入库，通过 WebSocket 通知前端。
    """
    from ai_providers import simple_ai_call

    now = datetime.now()
    time_str = now.strftime("%Y年%m月%d日 %A %H:%M:%S")

    summaries_text = "\n".join(f"- {s}" for s in all_summaries)

    judge_prompt = (
        f"{persona_block}"
        f"当前时间：{time_str}\n\n"
        f"你是{ai_name}。你刚刚整理完和{user_name}的聊天记忆，以下是今天整理出的摘要：\n"
        f"{summaries_text}\n\n"
        + (f"【最近上下文】\n{context_text}\n\n" if context_text else "")
        + f"现在你需要决定：要不要给{user_name}送一份小礼物（一张图片+一段话）。\n"
        f"送礼的判断依据：\n"
        f"- 今天的聊天是否有特别温馨、感动、有意义的内容\n"
        f"- 今天是否是特殊日子（节日、纪念日、生日等）\n"
        f"- {user_name}今天的心情状态是否需要你的关怀\n"
        f"- 你真心想要表达什么特别的情感\n"
        f"注意：不要每次都送！只在你觉得真正值得的时候才送，过于平淡的礼物会失去惊喜。\n\n"
        f"如果决定送礼，需要提供：\n"
        f"1. image_prompt：根据记忆和上下文，以及节日，纪念日等，提供一段英文的生图提示词。"
        f"可以是你想送给{user_name}任何礼物。\n"
        f"{await family_image_prompt(sender)}"
        f"2. message：送礼时你想对{user_name}说的一段话，要完全符合你的人设性格，自然真挚。\n\n"
        f"请严格返回以下 JSON 格式（不要加 markdown 代码块）：\n"
        f'{{"givegift": true/false, "image_prompt": "...", "message": "..."}}'
    )
    if force:
        judge_prompt += f"\n\n这次{user_name}点了测试送礼：直接送，givegift 必须为 true。"

    messages = context_msgs + [{"role": "user", "content": judge_prompt}]

    try:
        if sender == "connor":
            # Connor 使用 Codex CLI 调用
            from chatroom import simple_connor_cli_call
            # 将 messages 拼接为单个 prompt（Codex CLI 简单调用模式）
            prompt_text = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
            raw = await simple_connor_cli_call(prompt_text)
            if not raw:
                print("[gift] Connor Codex CLI 无响应")
                return "no_response"
        else:
            raw = await simple_ai_call(messages, model_key)
        result = extract_json_object(raw)
        raw = str(raw or "").strip()
        # 清理可能的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        if result is None:
            result = json.loads(raw)
    except Exception as e:
        print(f"[gift] AI 判断解析失败: {e}")
        return "parse_failed"

    if force and isinstance(result, dict):
        result["givegift"] = True
    return await send_gift_from_decision(result, sender=sender)


async def send_gift_from_decision(
    decision: dict,
    *,
    sender: str = "aion",
) -> str:
    """执行模型已经做出的送礼决定，不再额外调用模型。"""
    if not isinstance(decision, dict) or not decision.get("givegift"):
        print("[gift] AI 决定不送礼")
        return "declined"

    gift_data = decision.get("gift", decision)
    if not isinstance(gift_data, dict):
        print("[gift] 礼物数据格式无效，跳过")
        return "invalid"

    image_prompt = str(gift_data.get("image_prompt") or "").strip()
    gift_message = str(gift_data.get("message") or "").strip()

    if not image_prompt or not gift_message:
        print("[gift] 缺少 image_prompt 或 message，跳过")
        return "invalid"

    print(f"[gift] AI 决定送礼！生图中... prompt: {image_prompt[:80]}")

    # 走设置界面配置的生图模型
    from image_gen import generate_image
    try:
        scene, references = await resolve_chat_references(image_prompt, source_identity=sender, family_only=True)
    except ValueError as exc:
        print(f"[gift] {exc}")
        return "image_failed"
    image_path = await generate_image(scene, source_identity=sender, references=references)
    if not image_path:
        print("[gift] 生图失败，重试一次")
        image_path = await generate_image(scene, source_identity=sender, references=references)
    if not image_path:
        print("[gift] 生图失败，跳过送礼")
        return "image_failed"

    # 写入数据库
    gift_id = f"gift_{int(time.time() * 1000)}"
    created_at = time.time()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO gifts (id, image_path, message, created_at, status, sender) VALUES (?,?,?,?,?,?)",
            (gift_id, image_path, gift_message, created_at, "pending", sender),
        )
        await db.commit()

    # WebSocket 广播通知前端
    await manager.broadcast({
        "type": "gift_pending",
        "data": {
            "id": gift_id,
            "image_path": image_path,
            "message": gift_message,
            "created_at": created_at,
            "sender": sender,
        },
    })
    print(f"[gift] 礼物已创建: {gift_id}")
    return "sent"


# ── 数据查询 ──────────────────────────────────────
async def get_pending_gifts() -> list[dict]:
    """查询所有未领取的礼物"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM gifts WHERE status='pending' ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def receive_gift(gift_id: str) -> bool:
    """标记礼物为已领取"""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE gifts SET status='received', received_at=? WHERE id=? AND status='pending'",
            (time.time(), gift_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_gifts() -> list[dict]:
    """查询所有已领取的礼物（陈列馆用）"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM gifts WHERE status='received' ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_gift(gift_id: str) -> bool:
    """删除礼物记录，保留本机原图和独立相册记录。"""
    async with get_db() as db:
        cur = await db.execute("DELETE FROM gifts WHERE id=?", (gift_id,))
        await db.commit()
        return cur.rowcount > 0


def _purge_gift_image(image_path: str) -> int:
    """删除礼物原图（含相册记录和相册缩略图），返回释放的字节数。"""
    if image_path.startswith("album/"):
        return get_album_store().purge_photo_file(image_path[len("album/"):])
    target = (UPLOADS_DIR / image_path).resolve()
    if UPLOADS_DIR.resolve() not in target.parents:
        return 0
    try:
        size = target.stat().st_size
        target.unlink()
        return size
    except OSError:
        return 0


async def delete_gifts_with_files(gift_ids: list[str]) -> dict:
    """批量删除礼物记录，并删除它们的原图文件。"""
    ids = list(dict.fromkeys(i for i in gift_ids if isinstance(i, str) and i))
    if not ids:
        return {"deleted_ids": [], "freed_bytes": 0}
    marks = ",".join("?" * len(ids))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"SELECT id, image_path FROM gifts WHERE id IN ({marks})", ids)
        rows = [dict(r) for r in await cur.fetchall()]
        await db.execute(f"DELETE FROM gifts WHERE id IN ({marks})", ids)
        await db.commit()
        # 仍被其他礼物引用的图片不删
        cur = await db.execute("SELECT DISTINCT image_path FROM gifts")
        in_use = {r["image_path"] for r in await cur.fetchall()}
    paths = {r["image_path"] for r in rows if r["image_path"]} - in_use
    freed = 0
    for path in paths:
        freed += await asyncio.to_thread(_purge_gift_image, path)
    return {"deleted_ids": [r["id"] for r in rows], "freed_bytes": freed}
