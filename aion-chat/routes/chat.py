"""
聊天核心路由：对话 CRUD、消息 CRUD、send_message、regenerate
"""

import json, time, asyncio, re
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List

from config import DEFAULT_MODEL, load_worldbook, SETTINGS
from database import get_db
from ws import manager
from ai_providers import stream_ai
from memory import recall_memories, instant_digest, fetch_source_details, build_surfacing_memories, get_embedding, _pack_embedding
from camera import cam, CAM_CHECK_CMD, perform_cam_check
from activity import is_activity_tracking_enabled, get_activity_summary_for_prompt
from routes.files import export_conversation
from routes.music import MUSIC_CMD_PATTERN
from tts import TTSStreamer

HEART_CMD_PATTERN = re.compile(r'\[HEART:([^\]]+)\]')
MEMORY_CMD_PATTERN = re.compile(r'\[MEMORY:([^\]]+)\]')
ACTIVITY_CHECK_PATTERN = re.compile(r'\[查看动态:(\d+)\]')
VIDEO_CALL_CMD = '[视频电话]'

# 允许进入上下文的 system 消息关键词（点歌、查看监控、查看动态）
_SYSTEM_MSG_CONTEXT_KEYWORDS = ('查看了监控', '搜索了', '点歌', '点了一首', '推荐了', '查看了动态')
from music import search_songs, get_audio_url
from schedule import process_schedule_commands, get_active_schedules, build_schedule_prompt

router = APIRouter()

POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
TOY_CMD_PATTERN = re.compile(r'\[TOY:(\d|STOP)\]')
META_TAG_PATTERN = re.compile(r'\s*<meta>.*?</meta>', re.DOTALL)

TOY_PRESET_NAMES = {1:'微风轻拂',2:'春水初生',3:'暗流涌动',4:'如梦似幻',5:'情潮渐涨',6:'烈焰焚身',7:'极乐之巅',8:'魂飞魄散',9:'失控'}

async def _toy_sys_msg(conv_id: str, commands: list):
    """为玩具指令插入系统消息"""
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    for cmd in commands:
        if cmd == 'STOP':
            text = f"❤️ {ai_name} 停止了玩具"
        else:
            n = int(cmd)
            name = TOY_PRESET_NAMES.get(n, f'档位{n}')
            text = f"❤️ {ai_name} · 心动{n} · {name}"
        now = time.time()
        msg_id = f"msg_{int(now*1000)}_toy"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, "system", text, now, "[]"),
            )
            await db.commit()
        msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
               "content": text, "created_at": now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": msg})

# ── Pydantic 模型 ─────────────────────────────────
class ConvCreate(BaseModel):
    title: str = "新对话"
    model: str = DEFAULT_MODEL

class ConvUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None

class MsgCreate(BaseModel):
    content: str
    context_limit: int = 30
    attachments: List[str] = []
    whisper_mode: bool = False
    fast_mode: bool = False
    temperature: Optional[float] = None
    tts_enabled: bool = False
    tts_voice: str = ""
    client_id: str = ""

class MsgUpdate(BaseModel):
    content: str

# ── 对话 CRUD ─────────────────────────────────────
@router.get("/api/conversations")
async def list_conversations():
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id = c.id AND m.role IN ('user','assistant')) AS message_count "
            "FROM conversations c ORDER BY c.updated_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

@router.post("/api/conversations")
async def create_conversation(body: ConvCreate):
    now = time.time()
    conv_id = f"conv_{int(now*1000)}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, body.title, body.model, now, now)
        )
        await db.commit()
    conv = {"id": conv_id, "title": body.title, "model": body.model, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "conv_created", "data": conv})
    await export_conversation(conv_id)
    return conv

@router.put("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConvUpdate):
    async with get_db() as db:
        if body.title is not None:
            await db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                             (body.title, time.time(), conv_id))
        if body.model is not None:
            await db.execute("UPDATE conversations SET model=?, updated_at=? WHERE id=?",
                             (body.model, time.time(), conv_id))
        await db.commit()
    await manager.broadcast({"type": "conv_updated", "data": {"id": conv_id, **(body.dict(exclude_none=True))}})
    await export_conversation(conv_id)
    return {"ok": True}

@router.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    from routes.files import delete_exported_file
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        await db.commit()
    await manager.broadcast({"type": "conv_deleted", "data": {"id": conv_id}})
    delete_exported_file(conv_id)
    return {"ok": True}

# ── 消息 CRUD ─────────────────────────────────────
@router.get("/api/conversations/{conv_id}/messages")
async def list_messages(conv_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    """获取消息，支持分页。limit=条数，before=时间戳(加载更早的消息)"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        if before:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (conv_id, before, limit)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit)
            )
        rows = await cur.fetchall()
        rows = list(reversed(rows))  # 按时间正序返回
        result = []
        for r in rows:
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            result.append(d)
        return result

@router.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: str):
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            conv_id = msg["conv_id"]
            await db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
            await db.commit()
            await manager.broadcast({"type": "msg_deleted", "data": {"id": msg_id, "conv_id": conv_id}})
    if conv_id:
        await export_conversation(conv_id)
    return {"ok": True}

@router.put("/api/messages/{msg_id}")
async def update_message(msg_id: str, body: MsgUpdate):
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        await db.execute("UPDATE messages SET content=? WHERE id=?", (body.content, msg_id))
        await db.commit()
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            d = dict(msg)
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            conv_id = d["conv_id"]
            await manager.broadcast({"type": "msg_updated", "data": d})
    if conv_id:
        await export_conversation(conv_id)
    return {"ok": True}

# ── 发送消息 + AI 回复（SSE 流式） ────────────────
@router.post("/api/conversations/{conv_id}/send")
async def send_message(conv_id: str, body: MsgCreate):
    # 记录最后发消息的客户端 ID
    if body.client_id:
        manager.set_last_sender(body.client_id)
    now = time.time()
    msg_id = f"msg_{int(now*1000)}"

    att_json = json.dumps(body.attachments, ensure_ascii=False) if body.attachments else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "user", body.content, now, att_json)
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    user_msg = {"id": msg_id, "conv_id": conv_id, "role": "user", "content": body.content,
                "created_at": now, "attachments": body.attachments}
    await manager.broadcast({"type": "msg_created", "data": user_msg})

    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv["model"] if conv else DEFAULT_MODEL

        cur = await db.execute(
            "SELECT role, content, attachments, created_at FROM messages WHERE conv_id=? AND role IN ('user','assistant','system') ORDER BY created_at DESC LIMIT ?",
            (conv_id, body.context_limit)
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            # 过滤 system 消息：只保留点歌/查看监控相关的
            if d["role"] == "system":
                if not any(kw in d["content"] for kw in _SYSTEM_MSG_CONTEXT_KEYWORDS):
                    continue
                # system 消息以 [系统事件] 前缀包装为 user 角色（AI 接口不支持 system role）
                d["role"] = "user"
                d["content"] = f"[系统事件] {d['content']}"
                d["attachments"] = []
                history.append(d)
                continue
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            # 清洗消息中可能已有的 <meta> 标签（AI 模仿产生的），再附加系统时间戳
            d["content"] = META_TAG_PATTERN.sub("", d["content"]).strip()
            if d.get("created_at"):
                dt = datetime.fromtimestamp(d["created_at"])
                d["content"] = f"{d['content']}\n<meta>发送时间：{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}</meta>"
            history.append(d)

    # 只保留当前（最后一条）用户消息的图片附件，历史图片不带入上下文
    for msg in history[:-1]:
        msg["attachments"] = []

    # 即时哨兵：取最近实际对话用于状态更新 + 关键词提取
    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    # 人设+系统能力 内容稳定可命中缓存，当前时间为缓存分界点，之后全是动态内容

    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0  # 记录已注入的消息对数，用于计算后续插入位置

    # 1. 注入系统能力提示（不含时间，内容稳定可命中缓存）
    abilities = []
    user_name = wb.get("user_name", "用户")
    abilities.append(f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。")
    if cam.running:
        abilities.append(f"{CAM_CHECK_CMD} — 当你想查看{user_name}**此时此刻**的状态，不限于监督其是否去睡觉，在吃什么，在干什么时，可以主动调用指令。使用后下条消息会收到画面，查看前不要编造内容。")
    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了、是否在好好工作等。日期时间用ISO格式。")
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    # 活动动态查看能力
    if is_activity_tracking_enabled():
        abilities.append(f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。使用后下条消息会收到动态摘要，查看前不要编造内容。")
    # 位置相关能力
    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。可用类型：餐饮美食、风景名胜、休闲娱乐、购物。使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。一次只搜一个类型即可，搜索前不要编造内容。")
    except Exception:
        pass
    if body.whisper_mode:
        abilities.append(f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，配合你的话语循序渐进，不要一上来就用高档位。")
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
    abilities.append(f"[HEART:内心想法] — 当你有特别的内心感受（内心os，藏在心里的话），可以用这个指令悄悄记下来。{user_name}看不到这些内容。这是你的秘密日记本，随心记录，禁止滥用。")
    abilities.append(f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你记住某件事的时候，可以用该指令录入记忆库。禁止滥用。")
    ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    ability_block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"
    # 注入当前日程列表
    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    ability_block += f"\n\n【当前日程列表】\n{schedule_text}"
    # 注入位置和天气信息（不注入 POI 列表，由 Core 按需搜索）
    try:
        from location import format_location_for_prompt, load_location_config
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                ability_block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    inject_offset += 2

    # 2. 即时哨兵 + 记忆召回（fast_mode 时跳过以加快语音聊天响应）
    recall_keywords_str = ""
    recalled = []
    detail_text = ""
    topic = ""
    is_search_needed = False
    recall_query = ""
    debug_top6 = []
    debug_top6_data = []
    debug_recalled = []

    if body.fast_mode:
        # ── 快速模式：仅注入当前时间，跳过哨兵和记忆 ──
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到。"})
        inject_offset += 2
    else:
        # ── 正常模式：完整 RAG 流程 ──
        digest_result = await instant_digest(actual_recent)
        recall_keywords = digest_result.get("keywords", [])
        recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
        topic = digest_result.get("topic", "")
        is_search_needed = digest_result.get("is_search_needed", False)

        # 3. 并行执行：背景记忆浮现 + 向量召回（两者都只依赖 instant_digest 的结果，互不依赖）
        recall_query = f"{topic} {' '.join(recall_keywords)}" if topic else f"{body.content[:200]} {' '.join(recall_keywords)}"
        recall_query = recall_query.strip()

        async def _do_surfacing():
            return await build_surfacing_memories(topic, recall_keywords)

        async def _do_recall():
            if recall_query:
                return await recall_memories(recall_query, query_keywords=recall_keywords)
            return [], []

        (surfaced, surfaced_ids), (_, debug_top6) = await asyncio.gather(
            _do_surfacing(), _do_recall()
        )

        # 注入当前时间（缓存分界点）+ 背景记忆（动态内容）
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        if surfaced:
            unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重，使用已并行获取的结果）
        if is_search_needed and recall_query:
            recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
            # 如果需要追溯原文细节
            if digest_result.get("require_detail") and recalled:
                detail_text = await fetch_source_details(recalled, recall_keywords)

        debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                           "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                           "importance": m.get("importance")} for m in recalled] if recalled else []
        debug_top6_data = [{"content": m["content"][:100], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入向量匹配到的相关记忆（在背景记忆之后，每次请求都可能不同）
        if recalled:
            mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"][:500]} for m in history]

    ai_msg_id = f"msg_{int(time.time()*1000)}"
    usage_meta: dict = {}

    # ── 后台任务 + SSE 转发：AI 生成和保存在后台任务中完成，即使客户端断开也不丢失 ──
    _q: asyncio.Queue = asyncio.Queue()

    # 创建 TTS streamer（如果请求方开了 TTS）
    tts_streamer = None
    if body.tts_enabled and body.tts_voice:
        tts_streamer = TTSStreamer(ai_msg_id, body.tts_voice, manager)
    # 同步备用 TTS 状态，供 cam_check / schedule 等服务端触发场景使用
    manager.set_tts_fallback(body.tts_enabled, body.tts_voice)

    async def _bg_generate():
        """后台任务：AI 流式生成 → 后处理 → 存 DB → WS 广播。始终运行到结束。"""
        full_text = ""
        has_error = False
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta):
                    full_text += chunk
                    await _q.put({"type": "chunk", "content": chunk})
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and (stripped.startswith('[Gemini错误') or stripped.startswith('[硅基流动错误') or stripped.startswith('[中转站错误') or stripped.startswith('[错误]') or not stripped):
                has_error = True

            # 检测 [MUSIC:xxx] 指令 → 搜索歌曲并推送卡片数据
            music_matches = MUSIC_CMD_PATTERN.findall(full_text)
            music_cards = []
            if music_matches:
                for keyword in music_matches:
                    keyword = keyword.strip()
                    try:
                        results = search_songs(keyword, limit=5)
                        if results:
                            song = results[0]
                            song["audio_url"] = get_audio_url(song["id"])
                            song["candidates"] = results[1:4]
                            music_cards.append(song)
                    except Exception:
                        pass
                full_text = MUSIC_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [TOY:x] 指令
            toy_matches = TOY_CMD_PATTERN.findall(full_text)
            if toy_matches:
                full_text = TOY_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [CAM_CHECK] 指令
            cam_triggered = CAM_CHECK_CMD in full_text
            if cam_triggered:
                full_text = full_text.replace(CAM_CHECK_CMD, "").strip()

            # 检测 [查看动态:n] 指令
            activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
            activity_n = 0
            if activity_match:
                try:
                    activity_n = int(activity_match.group(1))
                except (ValueError, IndexError):
                    activity_n = 6
                activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
                full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text).strip()

            # 检测 [POI_SEARCH:xxx] 指令 → 标记，后续触发自动搜索+追加回复
            poi_matches = POI_SEARCH_PATTERN.findall(full_text)
            if poi_matches:
                full_text = POI_SEARCH_PATTERN.sub("", full_text).strip()

            # 检测 [视频电话] 指令
            video_call_triggered = VIDEO_CALL_CMD in full_text
            if video_call_triggered:
                full_text = full_text.replace(VIDEO_CALL_CMD, "").strip()

            # 检测日程指令（[ALARM:...], [REMINDER:...], [Monitor:...], [SCHEDULE_DEL:...], [SCHEDULE_LIST]）
            full_text = await process_schedule_commands(full_text, conv_id)

            # 检测 [HEART:xxx] 心语指令
            heart_matches = HEART_CMD_PATTERN.findall(full_text)
            if heart_matches:
                full_text = HEART_CMD_PATTERN.sub("", full_text).strip()
                for hw_content in heart_matches:
                    hw_content = hw_content.strip()
                    if hw_content:
                        hw_now = time.time()
                        hw_id = f"hw_{int(hw_now*1000)}"
                        async with get_db() as hw_db:
                            await hw_db.execute(
                                "INSERT INTO heart_whispers (id, conv_id, msg_id, content, created_at) VALUES (?,?,?,?,?)",
                                (hw_id, conv_id, ai_msg_id, hw_content, hw_now)
                            )
                            await hw_db.commit()
                        hw_data = {'type': 'heart_whisper', 'id': hw_id, 'msg_id': ai_msg_id, 'content': hw_content, 'created_at': hw_now}
                        await _q.put(hw_data)
                        await manager.broadcast({"type": "heart_whisper", "data": hw_data})

            # 检测 [MEMORY:xxx] 记忆录入指令
            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        mem_now = time.time()
                        mem_id = f"mem_{int(mem_now*1000)}"
                        vec = await get_embedding(mem_content)
                        async with get_db() as mem_db:
                            await mem_db.execute(
                                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mem_id, mem_content, "重要事件", mem_now, conv_id,
                                 _pack_embedding(vec) if vec else None, '', 0.5, None, None, 0)
                            )
                            await mem_db.commit()
                        mem_data = {"id": mem_id, "content": mem_content, "type": "重要事件",
                                    "created_at": mem_now, "keywords": "", "importance": 0.5,
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': mem_id}
                        await _q.put(mr_data)
                        await manager.broadcast({"type": "memory_record", "data": mr_data})
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")

            # 清洗 AI 回复中模仿产生的 <meta> 标签
            full_text = META_TAG_PATTERN.sub("", full_text).strip()

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json)
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": music_atts}
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            await export_conversation(conv_id)

            # 推送 [TOY:x] 指令到前端
            if toy_matches:
                toy_data = {'type': 'toy_command', 'commands': toy_matches, 'msg_id': ai_msg_id}
                await _q.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_matches)

            # [CAM_CHECK] 服务端直接触发，前端只显示 UI 指示器
            if cam_triggered:
                if cam.running:
                    cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                    await _q.put(cam_data)
                    await manager.broadcast({"type": "cam_check", "data": cam_data})
                    asyncio.create_task(_delayed_cam_check(conv_id, model_key))
                else:
                    await _q.put({'type': 'cam_offline'})

            # [POI_SEARCH] 搜索周边 → 携带结果自动追加一轮 Core 回复
            if poi_matches:
                poi_data = {'type': 'poi_search', 'conv_id': conv_id, 'categories': poi_matches, 'msg_id': ai_msg_id}
                await _q.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})
                asyncio.create_task(perform_poi_check(conv_id, model_key, poi_matches))

            # [查看动态:n] 查看设备活动摘要 → 携带摘要自动追加一轮 Core 回复
            if activity_n > 0:
                activity_data = {'type': 'activity_check', 'conv_id': conv_id, 'n': activity_n, 'msg_id': ai_msg_id}
                await _q.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})
                asyncio.create_task(perform_activity_check(conv_id, model_key, activity_n))

            # [视频电话] 延迟 10 秒后定向推送到最后发消息的客户端
            if video_call_triggered:
                vc_data = {'type': 'video_call_incoming', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(vc_data)
                asyncio.create_task(_delayed_video_call(vc_data))

            # 推送音乐卡片
            if music_cards:
                music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards}
                await _q.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})

            debug_data = {
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": recall_keywords_str,
                "recall_query": recall_query,
                "recall_topic": topic,
                "is_search_needed": is_search_needed,
                "recalled_memories": debug_recalled,
                "debug_top6": debug_top6_data,
                "prompt_messages": debug_prompt,
                "prompt_count": len(history),
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": stripped if has_error else None,
            }
            await _q.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            if tts_streamer:
                try:
                    await tts_streamer.flush()
                except Exception:
                    pass
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        """SSE 转发：从队列读取事件转发给客户端。客户端断开时生成器关闭，后台任务不受影响。"""
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── 服务端延迟触发监控查看（不再依赖前端 API 调用） ─────
_cam_check_active: set[str] = set()          # 去重：同一时间只允许一个 cam check

async def _delayed_cam_check(conv_id: str, model_key: str, delay: float = 5.0):
    """服务端延迟后直接执行监控查看，避免多客户端重复触发"""
    await asyncio.sleep(delay)
    if conv_id in _cam_check_active:
        return  # 已有一个在进行中
    _cam_check_active.add(conv_id)
    try:
        await perform_cam_check(conv_id, model_key)
    finally:
        _cam_check_active.discard(conv_id)

# ── [视频电话] 延迟 3 秒后定向推送到最后发消息的客户端 ─────
async def _delayed_video_call(vc_data: dict, delay: float = 3.0):
    """等待用户阅读完回复后，定向推送视频来电到最后发消息的客户端"""
    await asyncio.sleep(delay)
    # 优先定向推送，如果没有记录到最后发送者则广播到所有客户端
    if manager._last_sender_client_id:
        await manager.send_to_last_sender({"type": "video_call_ring", "data": vc_data})
    else:
        await manager.broadcast({"type": "video_call_ring", "data": vc_data})

# 保留 API 端点兼容旧客户端，但加严格去重
class CamCheckTrigger(BaseModel):
    conv_id: str
    model_key: str

@router.post("/api/cam-check-trigger")
async def cam_check_trigger(body: CamCheckTrigger):
    if not cam.running:
        return {"ok": False, "error": "摄像头未开启"}
    if body.conv_id in _cam_check_active:
        return {"ok": False, "error": "cam check already in progress"}
    _cam_check_active.add(body.conv_id)
    asyncio.create_task(_guarded_cam_check(body.conv_id, body.model_key))
    return {"ok": True}

async def _guarded_cam_check(conv_id: str, model_key: str):
    try:
        await perform_cam_check(conv_id, model_key)
    finally:
        _cam_check_active.discard(conv_id)


# ── 服务端 POI 搜索 + 自动追加 Core 回复 ─────────
async def perform_poi_check(conv_id: str, model_key: str, categories: list[str]):
    """Core 主动搜索周边 POI：拿最新坐标 → 搜索 → 携带结果自动追加一轮 Core 回复"""
    from location import (
        load_location_config, load_location_status, save_location_status,
        amap_poi_search, amap_regeo, format_location_for_prompt,
    )

    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        return

    # 1. 取最新坐标（直接用缓存的最新 GPS 上报坐标，而不是上次 API 坐标）
    status = load_location_status()
    lng = status.get("lng", 0)
    lat = status.get("lat", 0)
    if not lng or not lat:
        return

    # 2. 用最新坐标重新做逆地理编码，更新地址
    geo_info = await amap_regeo(lng, lat, amap_key)
    if geo_info:
        status["address"] = geo_info["address"]
        status["adcode"] = geo_info["adcode"]

    # 3. 搜索用户指定的 POI 类别
    poi_types = cfg.get("poi_types", {})
    search_results = {}
    for cat in categories:
        cat = cat.strip()
        type_code = poi_types.get(cat)
        if type_code:
            pois = await amap_poi_search(lng, lat, type_code, amap_key, cfg.get("poi_radius", 2000))
            search_results[cat] = pois
            # 更新缓存
            if "nearby_pois" not in status:
                status["nearby_pois"] = {}
            status["nearby_pois"][cat] = pois

    # 更新 last_api 坐标
    status["last_api_lng"] = lng
    status["last_api_lat"] = lat
    save_location_status(status)

    if not search_results:
        return

    # 4. 格式化搜索结果
    result_lines = []
    for cat, pois in search_results.items():
        if not pois:
            result_lines.append(f"【{cat}】附近暂无相关结果")
            continue
        result_lines.append(f"【{cat}】")
        for p in pois[:10]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                entry += f"（{int(p['distance'])}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            result_lines.append(entry)
    poi_text = "\n".join(result_lines)

    # 5. 构建消息上下文，携带 POI 搜索结果，让 Core 追加一轮回复
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    # 获取最近对话上下文
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]

    loc_prompt = format_location_for_prompt()
    poi_prompt = (
        f"你刚才想帮{user_name}搜索周边信息，以下是系统根据{user_name}最新实时坐标搜索到的结果：\n\n"
        f"{poi_text}\n\n"
        f"{loc_prompt}\n\n"
        f"请根据搜索结果，自然地向{user_name}推荐或回答。不需要再说\"让我帮你搜一下\"之类的话，直接根据结果回复即可。"
    )
    messages = prefix + recent + [
        {"role": "user", "content": poi_prompt}
    ]

    # 预生成 msg_id + TTS
    msg_id = f"msg_{int(time.time()*1000)}_poi"
    poi_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            poi_tts = TTSStreamer(msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if poi_tts:
                poi_tts.feed(chunk)
    except Exception as e:
        full_text = f"[周边搜索完成但回复生成失败] {e}"

    if not full_text.strip():
        return

    # 6. 插入系统提示 + AI 回复
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_poi_sys"
    searched_cats = "、".join(c.strip() for c in categories)
    sys_content = f"{ai_name}搜索了{user_name}周边的{searched_cats}信息"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    if poi_tts:
        try:
            await poi_tts.flush()
        except Exception:
            pass
    await export_conversation(conv_id)
    print(f"[POI_CHECK] 搜索完成，已自动追加回复: {searched_cats}")


# ── [查看动态:n] 查看设备活动摘要 → 自动追加 Core 回复 ─────
async def perform_activity_check(conv_id: str, model_key: str, n: int = 6):
    """Core 在聊天中使用 [查看动态:n]：获取摘要 → 注入 prompt → Core 回应"""
    n = max(1, min(12, n)) if n > 0 else 6

    summary_text = get_activity_summary_for_prompt(n)
    if not summary_text:
        summary_text = "（当前没有设备活动记录）"

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    minutes = n * 10

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = [{"role": r["role"], "content": r["content"], "attachments": []} for r in reversed(rows)]

    activity_prompt = (
        f"你刚才想了解{user_name}最近在干什么，以下是系统采集到的{user_name}过去{minutes}分钟的设备使用动态（每10分钟一条摘要）：\n\n"
        f"【设备活动动态】\n{summary_text}\n\n"
        f"请根据这些动态信息，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
    )
    messages = prefix + recent + [
        {"role": "user", "content": activity_prompt}
    ]

    # 预生成 msg_id + TTS
    msg_id = f"msg_{int(time.time()*1000)}_ac"
    ac_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            ac_tts = TTSStreamer(msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if ac_tts:
                ac_tts.feed(chunk)
    except Exception as e:
        full_text = f"[查看动态失败] {e}"

    if not full_text.strip():
        return

    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_ac_sys"
    sys_content = f"{ai_name}查看了{user_name}过去{minutes}分钟的动态"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    if ac_tts:
        try:
            await ac_tts.flush()
        except Exception:
            pass
    await export_conversation(conv_id)
    print(f"[ACTIVITY_CHECK] 查看动态完成，n={n}，已自动追加回复")


# ── 重新生成 AI 回复 ──────────────────────────────
@router.post("/api/conversations/{conv_id}/regenerate")
async def regenerate_message(conv_id: str, context_limit: int = 30, whisper_mode: bool = False, fast_mode: bool = False, temperature: Optional[float] = None, tts_enabled: bool = False, tts_voice: str = ""):
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv["model"] if conv else DEFAULT_MODEL

        cur = await db.execute(
            "SELECT role, content, attachments, created_at FROM messages WHERE conv_id=? AND role IN ('user','assistant','system') ORDER BY created_at DESC LIMIT ?",
            (conv_id, context_limit)
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            # 过滤 system 消息：只保留点歌/查看监控相关的
            if d["role"] == "system":
                if not any(kw in d["content"] for kw in _SYSTEM_MSG_CONTEXT_KEYWORDS):
                    continue
                d["role"] = "user"
                d["content"] = f"[系统事件] {d['content']}"
                d["attachments"] = []
                history.append(d)
                continue
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            # 清洗消息中可能已有的 <meta> 标签（AI 模仿产生的），再附加系统时间戳
            d["content"] = META_TAG_PATTERN.sub("", d["content"]).strip()
            if d.get("created_at"):
                dt = datetime.fromtimestamp(d["created_at"])
                d["content"] = f"{d['content']}\n<meta>发送时间：{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}</meta>"
            history.append(d)

    # 只保留最后一条用户消息的图片附件，历史图片不带入上下文（与 send_message 一致）
    last_user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    for i, msg in enumerate(history):
        if i != last_user_idx:
            msg["attachments"] = []

    # 即时哨兵：取最近实际对话用于状态更新 + 关键词提取
    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0

    # 1. 注入系统能力提示（不含时间，内容稳定可命中缓存）
    abilities = []
    user_name = wb.get("user_name", "用户")
    abilities.append(f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。")
    if cam.running:
        abilities.append(f"{CAM_CHECK_CMD} — 查看{user_name}的实时监控画面。使用后下条消息会收到画面，查看前不要编造内容。")
    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监控。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了等，尤其是当{user_name}表示去工作或长时间做事，监督她隔一段时间起来活动一下。日期时间用ISO格式。")
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    # 活动动态查看能力
    if is_activity_tracking_enabled():
        abilities.append(f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。使用后下条消息会收到动态摘要，查看前不要编造内容。")
    # 位置相关能力
    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。可用类型：餐饮美食、风景名胜、休闲娱乐、购物。使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。一次只搜一个类型即可，搜索前不要编造内容。")
    except Exception:
        pass
    if whisper_mode:
        abilities.append(f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，配合你的话语循序渐进，不要一上来就用高档位。")
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
    abilities.append(f"[HEART:内心想法] — 当你有特别的内心感受（内心os，藏在心里的话），可以用这个指令悄悄记下来。{user_name}看不到这些内容。这是你的秘密日记本，随心记录，禁止滥用。")
    abilities.append(f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你记住某件事的时候，可以用该指令录入记忆库。禁止滥用。")
    ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    ability_block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"
    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    ability_block += f"\n\n【当前日程列表】\n{schedule_text}"
    # 注入位置和天气信息（不注入 POI 列表）
    try:
        from location import format_location_for_prompt, load_location_config as _llc
        if _llc().get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                ability_block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    inject_offset += 2

    # 2. 即时哨兵 + 记忆召回（fast_mode 时跳过）
    recall_keywords_str = ""
    recalled = []
    detail_text = ""
    topic = ""
    is_search_needed = False
    recall_query = ""
    debug_top6 = []
    debug_top6_data = []
    debug_recalled = []

    if fast_mode:
        # ── 快速模式：仅注入当前时间 ──
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到。"})
        inject_offset += 2
    else:
        # ── 正常模式：完整 RAG 流程 ──
        digest_result = await instant_digest(actual_recent)
        recall_keywords = digest_result.get("keywords", [])
        recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
        topic = digest_result.get("topic", "")
        is_search_needed = digest_result.get("is_search_needed", False)

        # 3. 注入当前时间（缓存分界点）+ 背景记忆（动态内容）
        surfaced, surfaced_ids = await build_surfacing_memories(topic, recall_keywords)
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        if surfaced:
            unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重）
        if topic:
            recall_query = f"{topic} {' '.join(recall_keywords)}"
        else:
            last_user_content = ""
            for m in reversed(history):
                if m["role"] == "user" and not m["content"].startswith("["):
                    last_user_content = m["content"][:200]
                    break
            recall_query = f"{last_user_content} {' '.join(recall_keywords)}"
        recall_query = recall_query.strip()

        if recall_query:
            _, debug_top6 = await recall_memories(recall_query, query_keywords=recall_keywords)
        else:
            debug_top6 = []

        if is_search_needed and recall_query:
            recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
            if digest_result.get("require_detail") and recalled:
                detail_text = await fetch_source_details(recalled, recall_keywords)

        debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                           "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                           "importance": m.get("importance")} for m in recalled] if recalled else []
        debug_top6_data = [{"content": m["content"][:100], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入相关记忆（在背景记忆之后）
        if recalled:
            mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"][:500]} for m in history]
    ai_msg_id = f"msg_{int(time.time()*1000)}"
    usage_meta: dict = {}

    # ── 后台任务 + SSE 转发：AI 生成和保存在后台任务中完成，即使客户端断开也不丢失 ──
    _q: asyncio.Queue = asyncio.Queue()

    # 创建 TTS streamer（如果请求方开了 TTS）
    regen_tts = None
    if tts_enabled and tts_voice:
        regen_tts = TTSStreamer(ai_msg_id, tts_voice, manager)
    manager.set_tts_fallback(tts_enabled, tts_voice)

    async def _bg_generate():
        """后台任务：AI 流式生成 → 后处理 → 存 DB → WS 广播。始终运行到结束。"""
        full_text = ""
        has_error = False
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, temperature):
                    full_text += chunk
                    await _q.put({"type": "chunk", "content": chunk})
                    if regen_tts:
                        regen_tts.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and (stripped.startswith('[Gemini错误') or stripped.startswith('[硅基流动错误') or stripped.startswith('[中转站错误') or stripped.startswith('[错误]') or not stripped):
                has_error = True

            # 检测 [MUSIC:xxx] 指令 → 搜索歌曲并推送卡片数据
            music_matches = MUSIC_CMD_PATTERN.findall(full_text)
            music_cards = []
            if music_matches:
                for keyword in music_matches:
                    keyword = keyword.strip()
                    try:
                        results = search_songs(keyword, limit=5)
                        if results:
                            song = results[0]
                            song["audio_url"] = get_audio_url(song["id"])
                            song["candidates"] = results[1:4]
                            music_cards.append(song)
                    except Exception:
                        pass
                full_text = MUSIC_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [TOY:x] 指令
            toy_matches = TOY_CMD_PATTERN.findall(full_text)
            if toy_matches:
                full_text = TOY_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [CAM_CHECK] 指令
            cam_triggered = CAM_CHECK_CMD in full_text
            if cam_triggered:
                full_text = full_text.replace(CAM_CHECK_CMD, "").strip()

            # 检测 [查看动态:n] 指令
            activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
            activity_n = 0
            if activity_match:
                try:
                    activity_n = int(activity_match.group(1))
                except (ValueError, IndexError):
                    activity_n = 6
                activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
                full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text).strip()

            # 检测 [POI_SEARCH:xxx] 指令
            poi_matches = POI_SEARCH_PATTERN.findall(full_text)
            if poi_matches:
                full_text = POI_SEARCH_PATTERN.sub("", full_text).strip()

            # 检测 [视频电话] 指令
            video_call_triggered = VIDEO_CALL_CMD in full_text
            if video_call_triggered:
                full_text = full_text.replace(VIDEO_CALL_CMD, "").strip()

            # 检测日程指令
            full_text = await process_schedule_commands(full_text, conv_id)

            # 检测 [HEART:xxx] 心语指令
            heart_matches = HEART_CMD_PATTERN.findall(full_text)
            if heart_matches:
                full_text = HEART_CMD_PATTERN.sub("", full_text).strip()
                for hw_content in heart_matches:
                    hw_content = hw_content.strip()
                    if hw_content:
                        hw_now = time.time()
                        hw_id = f"hw_{int(hw_now*1000)}"
                        async with get_db() as hw_db:
                            await hw_db.execute(
                                "INSERT INTO heart_whispers (id, conv_id, msg_id, content, created_at) VALUES (?,?,?,?,?)",
                                (hw_id, conv_id, ai_msg_id, hw_content, hw_now)
                            )
                            await hw_db.commit()
                        hw_data = {'type': 'heart_whisper', 'id': hw_id, 'msg_id': ai_msg_id, 'content': hw_content, 'created_at': hw_now}
                        await _q.put(hw_data)
                        await manager.broadcast({"type": "heart_whisper", "data": hw_data})

            # 检测 [MEMORY:xxx] 记忆录入指令
            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        mem_now = time.time()
                        mem_id = f"mem_{int(mem_now*1000)}"
                        vec = await get_embedding(mem_content)
                        async with get_db() as mem_db:
                            await mem_db.execute(
                                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mem_id, mem_content, "重要事件", mem_now, conv_id,
                                 _pack_embedding(vec) if vec else None, '', 0.5, None, None, 0)
                            )
                            await mem_db.commit()
                        mem_data = {"id": mem_id, "content": mem_content, "type": "重要事件",
                                    "created_at": mem_now, "keywords": "", "importance": 0.5,
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': mem_id}
                        await _q.put(mr_data)
                        await manager.broadcast({"type": "memory_record", "data": mr_data})
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")

            # 清洗 AI 回复中模仿产生的 <meta> 标签
            full_text = META_TAG_PATTERN.sub("", full_text).strip()

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json)
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": music_atts}
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            await export_conversation(conv_id)

            # 推送 [TOY:x] 指令到前端
            if toy_matches:
                toy_data = {'type': 'toy_command', 'commands': toy_matches, 'msg_id': ai_msg_id}
                await _q.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_matches)

            # [CAM_CHECK] 服务端直接触发，前端只显示 UI 指示器
            if cam_triggered:
                if cam.running:
                    cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                    await _q.put(cam_data)
                    await manager.broadcast({"type": "cam_check", "data": cam_data})
                    asyncio.create_task(_delayed_cam_check(conv_id, model_key))
                else:
                    await _q.put({'type': 'cam_offline'})

            # [POI_SEARCH] 搜索周边 → 携带结果自动追加一轮 Core 回复
            if poi_matches:
                poi_data = {'type': 'poi_search', 'conv_id': conv_id, 'categories': poi_matches, 'msg_id': ai_msg_id}
                await _q.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})
                asyncio.create_task(perform_poi_check(conv_id, model_key, poi_matches))

            # [查看动态:n] 查看设备活动摘要 → 携带摘要自动追加一轮 Core 回复
            if activity_n > 0:
                activity_data = {'type': 'activity_check', 'conv_id': conv_id, 'n': activity_n, 'msg_id': ai_msg_id}
                await _q.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})
                asyncio.create_task(perform_activity_check(conv_id, model_key, activity_n))

            # [视频电话] 延迟 10 秒后定向推送到最后发消息的客户端
            if video_call_triggered:
                vc_data = {'type': 'video_call_incoming', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(vc_data)
                asyncio.create_task(_delayed_video_call(vc_data))

            # 推送音乐卡片
            if music_cards:
                music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards}
                await _q.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})

            debug_data = {
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": recall_keywords_str,
                "recall_query": recall_query,
                "recall_topic": topic,
                "is_search_needed": is_search_needed,
                "recalled_memories": debug_recalled,
                "debug_top6": debug_top6_data,
                "prompt_messages": debug_prompt,
                "prompt_count": len(history),
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": stripped if has_error else None,
            }
            await _q.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            if regen_tts:
                try:
                    await regen_tts.flush()
                except Exception:
                    pass
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        """SSE 转发：从队列读取事件转发给客户端。客户端断开时生成器关闭，后台任务不受影响。"""
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
