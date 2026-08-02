"""
小剧场路由：完全独立于主聊天，不涉及记忆库 / 系统能力 / 日程 / 摄像头等
仅保留：对话 CRUD、消息 CRUD、SSE 流式 AI 回复、TTS
"""

import json, time, asyncio, uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DEFAULT_MODEL, DATA_DIR, SETTINGS, THEATER_TTS_CACHE_DIR, THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from tts import TTSStreamer

router = APIRouter(prefix="/api/theater", tags=["theater"])

THEATER_TTS_MIN_CHARS = 300
THEATER_TTS_MAX_CHARS = 500
THEATER_TTS_AUDIO_PREFIX = "/api/theater/tts/audio"

# ── 角色预设文件路径 ──
PERSONAS_PATH = DATA_DIR / "theater_personas.json"


def _load_personas() -> list:
    if PERSONAS_PATH.exists():
        try:
            return json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_personas(data: list):
    PERSONAS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Pydantic 模型 ──
class PersonaCreate(BaseModel):
    name: str = "新角色"
    persona: str = ""


class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    persona: Optional[str] = None


class ConvCreate(BaseModel):
    title: str = "新剧场"
    persona_id: str = ""
    model: str = DEFAULT_MODEL


class ConvUpdate(BaseModel):
    title: Optional[str] = None
    persona_id: Optional[str] = None
    model: Optional[str] = None


class MsgCreate(BaseModel):
    content: str
    context_limit: int = 20
    attachments: List[str] = []
    temperature: Optional[float] = None
    tts_enabled: bool = False
    tts_voice: str = ""


class MsgUpdate(BaseModel):
    content: str


# ══════════════════════════════════════════════════
#  角色 CRUD
# ══════════════════════════════════════════════════

@router.get("/personas")
async def list_personas():
    return _load_personas()


@router.post("/personas")
async def create_persona(body: PersonaCreate):
    personas = _load_personas()
    p = {
        "id": str(uuid.uuid4())[:8],
        "name": body.name,
        "persona": body.persona,
        "created_at": time.time(),
    }
    personas.append(p)
    _save_personas(personas)
    return p


@router.put("/personas/{pid}")
async def update_persona(pid: str, body: PersonaUpdate):
    personas = _load_personas()
    for p in personas:
        if p["id"] == pid:
            if body.name is not None:
                p["name"] = body.name
            if body.persona is not None:
                p["persona"] = body.persona
            _save_personas(personas)
            return p
    return {"error": "not found"}


@router.delete("/personas/{pid}")
async def delete_persona(pid: str):
    personas = _load_personas()
    personas = [p for p in personas if p["id"] != pid]
    _save_personas(personas)
    return {"ok": True}


# ══════════════════════════════════════════════════
#  对话 CRUD
# ══════════════════════════════════════════════════

@router.get("/conversations")
async def list_conversations():
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM theater_messages m WHERE m.conv_id = c.id AND m.role IN ('user','assistant')) AS message_count "
            "FROM theater_conversations c ORDER BY c.updated_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@router.post("/conversations")
async def create_conversation(body: ConvCreate):
    now = time.time()
    conv_id = f"tc_{int(now * 1000)}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO theater_conversations (id, title, persona_id, model, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (conv_id, body.title, body.persona_id, body.model, now, now),
        )
        await db.commit()
    conv = {"id": conv_id, "title": body.title, "persona_id": body.persona_id,
            "model": body.model, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "theater_conv_created", "data": conv})
    return conv


@router.put("/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConvUpdate):
    async with get_db() as db:
        if body.title is not None:
            await db.execute("UPDATE theater_conversations SET title=?, updated_at=? WHERE id=?",
                             (body.title, time.time(), conv_id))
        if body.persona_id is not None:
            await db.execute("UPDATE theater_conversations SET persona_id=?, updated_at=? WHERE id=?",
                             (body.persona_id, time.time(), conv_id))
        if body.model is not None:
            await db.execute("UPDATE theater_conversations SET model=?, updated_at=? WHERE id=?",
                             (body.model, time.time(), conv_id))
        await db.commit()
    await manager.broadcast({"type": "theater_conv_updated", "data": {"id": conv_id, **(body.dict(exclude_none=True))}})
    return {"ok": True}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM theater_conversations WHERE id=?", (conv_id,))
        await db.commit()
    await manager.broadcast({"type": "theater_conv_deleted", "data": {"id": conv_id}})
    return {"ok": True}


# ══════════════════════════════════════════════════
#  消息 CRUD
# ══════════════════════════════════════════════════

@router.get("/conversations/{conv_id}/messages")
async def list_messages(conv_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        if before:
            cur = await db.execute(
                "SELECT * FROM theater_messages WHERE conv_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (conv_id, before, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM theater_messages WHERE conv_id=? ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit),
            )
        rows = await cur.fetchall()
        rows = list(reversed(rows))
        result = []
        for r in rows:
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            result.append(d)
        return result


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: str):
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT conv_id FROM theater_messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM theater_messages WHERE id=?", (msg_id,))
            await db.commit()
            await manager.broadcast({"type": "theater_msg_deleted", "data": {"id": msg_id, "conv_id": row["conv_id"]}})
    return {"ok": True}


@router.put("/messages/{msg_id}")
async def update_message(msg_id: str, body: MsgUpdate):
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        await db.execute("UPDATE theater_messages SET content=? WHERE id=?", (body.content, msg_id))
        await db.commit()
        cur = await db.execute("SELECT * FROM theater_messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            d = dict(msg)
            try:
                d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except Exception:
                d["attachments"] = []
            await manager.broadcast({"type": "theater_msg_updated", "data": d})
    return {"ok": True}


# ══════════════════════════════════════════════════
#  发送消息 + AI 流式回复（SSE）
# ══════════════════════════════════════════════════

@router.post("/conversations/{conv_id}/send")
async def send_message(conv_id: str, body: MsgCreate):
    now = time.time()
    msg_id = f"tm_{int(now * 1000)}"

    att_json = json.dumps(body.attachments, ensure_ascii=False) if body.attachments else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO theater_messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "user", body.content, now, att_json),
        )
        await db.execute("UPDATE theater_conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    user_msg = {"id": msg_id, "conv_id": conv_id, "role": "user",
                "content": body.content, "created_at": now, "attachments": body.attachments}
    await manager.broadcast({"type": "theater_msg_created", "data": user_msg})

    # 读取对话的模型和角色
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT model, persona_id FROM theater_conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv["model"] if conv else DEFAULT_MODEL
        persona_id = conv["persona_id"] if conv else ""

        # 读上下文
        cur = await db.execute(
            "SELECT role, content, attachments FROM theater_messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT ?",
            (conv_id, body.context_limit),
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            try:
                d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except Exception:
                d["attachments"] = []
            history.append(d)

    # 只保留最后一条用户消息的附件
    for msg in history[:-1]:
        msg["attachments"] = []

    # 加载角色人设
    persona_text = ""
    temperature = body.temperature
    personas = _load_personas()
    for p in personas:
        if p["id"] == persona_id:
            persona_text = p.get("persona", "")
            break

    # 构建 prompt：仅注入人设 + 上下文，干净纯粹
    prefix = []
    if persona_text:
        prefix.append({"role": "user", "content": f"[角色设定]\n{persona_text}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if prefix:
        history = prefix + history

    ai_msg_id = f"tm_{int(time.time() * 1000)}_ai"
    usage_meta: dict = {}
    _q: asyncio.Queue = asyncio.Queue()

    tts_streamer = None
    if body.tts_enabled and body.tts_voice:
        tts_streamer = TTSStreamer(
            ai_msg_id,
            body.tts_voice,
            manager,
            min_chars=THEATER_TTS_MIN_CHARS,
            max_chars=THEATER_TTS_MAX_CHARS,
            cache_dir=THEATER_TTS_CACHE_DIR,
            audio_url_prefix=THEATER_TTS_AUDIO_PREFIX,
            merge_segments=True,
            delete_segments_after_seconds=THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS,
            cache_max_bytes=None,
        )

    async def _bg_generate():
        full_text = ""
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, temperature=temperature):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    full_text += chunk
                    await _q.put({"type": "chunk", "content": chunk})
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            full_text = full_text.strip()

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO theater_messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, "[]"),
                )
                await db2.execute("UPDATE theater_conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                      "content": full_text, "created_at": now2, "attachments": []}
            await manager.broadcast({"type": "theater_msg_created", "data": ai_msg})

            # debug 信息（精简版）
            await _q.put({
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "usage": usage_meta if usage_meta else None,
            })
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
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 重新生成 ──
@router.post("/conversations/{conv_id}/regenerate")
async def regenerate_message(conv_id: str, context_limit: int = 20, temperature: Optional[float] = None,
                             tts_enabled: bool = False, tts_voice: str = ""):
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT model, persona_id FROM theater_conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv["model"] if conv else DEFAULT_MODEL
        persona_id = conv["persona_id"] if conv else ""

        cur = await db.execute(
            "SELECT role, content, attachments FROM theater_messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT ?",
            (conv_id, context_limit),
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            try:
                d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except Exception:
                d["attachments"] = []
            history.append(d)

    # 只保留最后一条用户消息的附件
    last_user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    for i, msg in enumerate(history):
        if i != last_user_idx:
            msg["attachments"] = []

    # 加载角色人设
    persona_text = ""
    personas = _load_personas()
    for p in personas:
        if p["id"] == persona_id:
            persona_text = p.get("persona", "")
            break

    prefix = []
    if persona_text:
        prefix.append({"role": "user", "content": f"[角色设定]\n{persona_text}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if prefix:
        history = prefix + history

    ai_msg_id = f"tm_{int(time.time() * 1000)}_regen"
    usage_meta: dict = {}
    _q: asyncio.Queue = asyncio.Queue()

    tts_streamer = None
    if tts_enabled and tts_voice:
        tts_streamer = TTSStreamer(
            ai_msg_id,
            tts_voice,
            manager,
            min_chars=THEATER_TTS_MIN_CHARS,
            max_chars=THEATER_TTS_MAX_CHARS,
            cache_dir=THEATER_TTS_CACHE_DIR,
            audio_url_prefix=THEATER_TTS_AUDIO_PREFIX,
            merge_segments=True,
            delete_segments_after_seconds=THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS,
            cache_max_bytes=None,
        )

    async def _bg_generate():
        full_text = ""
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, temperature=temperature):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    full_text += chunk
                    await _q.put({"type": "chunk", "content": chunk})
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            full_text = full_text.strip()

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO theater_messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, "[]"),
                )
                await db2.execute("UPDATE theater_conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                      "content": full_text, "created_at": now2, "attachments": []}
            await manager.broadcast({"type": "theater_msg_created", "data": ai_msg})

            await _q.put({
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "usage": usage_meta if usage_meta else None,
            })
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
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
