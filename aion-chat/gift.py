"""
礼物系统：AI 判断送礼 + 硅基流动 Kolors 生图 + 礼物数据 CRUD
"""

import json, time
from datetime import datetime

import httpx, aiosqlite

from config import get_key, UPLOADS_DIR, load_worldbook
from database import get_db
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
):
    """
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
        f"现在你需要决定：要不要给{user_name}送一份小礼物（一张图片+一段话）。\n"
        f"送礼的判断依据：\n"
        f"- 今天的聊天是否有特别温馨、感动、有意义的内容\n"
        f"- 今天是否是特殊日子（节日、纪念日、生日等）\n"
        f"- {user_name}今天的心情状态是否需要你的关怀\n"
        f"- 你真心想要表达什么特别的情感\n"
        f"注意：不要每次都送！只在你觉得真正值得的时候才送，过于平淡的礼物会失去惊喜。\n\n"
        f"如果决定送礼，需要提供：\n"
        f"1. image_prompt：根据记忆和上下文，以及节日，纪念日等，提供一段英文的生图提示词。"
        f"可以是你想送给{user_name}任何礼物。\n"
        f"2. message：送礼时你想对{user_name}说的一段话，要完全符合你的人设性格，自然真挚。\n\n"
        f"请严格返回以下 JSON 格式（不要加 markdown 代码块）：\n"
        f'{{"givegift": true/false, "image_prompt": "...", "message": "..."}}'
    )

    messages = context_msgs + [{"role": "user", "content": judge_prompt}]

    try:
        raw = await simple_ai_call(messages, model_key)
        raw = raw.strip()
        # 清理可能的 markdown 代码块
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
    except Exception as e:
        print(f"[gift] AI 判断解析失败: {e}")
        return

    if not result.get("givegift"):
        print("[gift] AI 决定不送礼")
        return

    image_prompt = result.get("image_prompt", "").strip()
    gift_message = result.get("message", "").strip()

    if not image_prompt or not gift_message:
        print("[gift] 缺少 image_prompt 或 message，跳过")
        return

    print(f"[gift] AI 决定送礼！生图中... prompt: {image_prompt[:80]}")

    # 调用硅基流动 Kolors 生图
    image_path = await _generate_image(image_prompt)
    if not image_path:
        print("[gift] 生图失败，跳过送礼")
        return

    # 写入数据库
    gift_id = f"gift_{int(time.time() * 1000)}"
    created_at = time.time()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO gifts (id, image_path, message, created_at, status) VALUES (?,?,?,?,?)",
            (gift_id, image_path, gift_message, created_at, "pending"),
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
        },
    })
    print(f"[gift] 礼物已创建: {gift_id}")


# ── 硅基流动 Kolors 生图 ──────────────────────────
async def _generate_image(prompt: str) -> str | None:
    """调用硅基流动 Kwai-Kolors/Kolors 生成图片，下载保存到本地，返回相对路径"""
    api_key = get_key("siliconflow")
    if not api_key:
        print("[gift] 没有硅基流动 API Key，无法生图")
        return None

    url = "https://api.siliconflow.cn/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "Kwai-Kolors/Kolors",
        "prompt": prompt,
        "negative_prompt": "realistic human, real person, photo, ugly, blurry, low quality, deformed",
        "image_size": "1024x1024",
        "batch_size": 1,
        "num_inference_steps": 20,
        "guidance_scale": 7.5,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            images = data.get("images", [])
            if not images:
                print("[gift] 生图 API 返回空 images")
                return None

            image_url = images[0].get("url", "")
            if not image_url:
                print("[gift] 生图 API 返回空 URL")
                return None

            # 下载图片（URL 1 小时过期，必须立即下载）
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()

            filename = f"gift_{int(time.time() * 1000)}.png"
            filepath = UPLOADS_DIR / filename
            filepath.write_bytes(img_resp.content)
            print(f"[gift] 图片已保存: {filepath}")
            return filename

    except Exception as e:
        print(f"[gift] 生图异常: {e}")
        return None


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
    """删除礼物"""
    async with get_db() as db:
        # 获取图片路径以便清理
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT image_path FROM gifts WHERE id=?", (gift_id,))
        row = await cur.fetchone()
        if row:
            img_path = UPLOADS_DIR / row["image_path"]
            if img_path.exists():
                try:
                    img_path.unlink()
                except Exception:
                    pass
        cur = await db.execute("DELETE FROM gifts WHERE id=?", (gift_id,))
        await db.commit()
        return cur.rowcount > 0
