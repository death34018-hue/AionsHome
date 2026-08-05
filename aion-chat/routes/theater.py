"""
小剧场路由：完全独立于主聊天，不涉及记忆库 / 系统能力 / 日程 / 摄像头等
仅保留：对话 CRUD、消息 CRUD、SSE 流式 AI 回复、TTS
"""

import json, time, asyncio, uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DEFAULT_MODEL, DATA_DIR, SETTINGS, THEATER_TTS_CACHE_DIR, THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from stream_safety import (
    THEATER_STREAM_POLICY,
    StreamSafetyResult,
    consume_safe_stream,
)
from tts import TTSStreamer
from theater_tts_cache import delete_message_audio_files, list_message_audio_segments

router = APIRouter(prefix="/api/theater", tags=["theater"])

THEATER_TTS_MIN_CHARS = 300
THEATER_TTS_MAX_CHARS = 500
THEATER_TTS_AUDIO_PREFIX = "/api/theater/tts/audio"
_active_theater_tts: dict[str, tuple[str, TTSStreamer]] = {}
_deleted_theater_conversations: set[str] = set()


async def _consume_theater_stream(source, queue, tts_streamer=None) -> StreamSafetyResult:
    async def on_commit(chunk: str) -> None:
        await queue.put({"type": "chunk", "content": chunk})
        if tts_streamer:
            await tts_streamer.feed_async(chunk)

    result = await consume_safe_stream(
        source,
        THEATER_STREAM_POLICY,
        on_commit,
    )
    if result.notice:
        await queue.put({"type": "chunk", "content": f"\n\n[{result.notice}]"})
    return result


@router.get("/tts/segments/{msg_id}")
async def list_tts_segments(msg_id: str):
    segments = list_message_audio_segments(msg_id, THEATER_TTS_CACHE_DIR)
    return {
        "segments": [
            {"seq": seq, "url": f"{THEATER_TTS_AUDIO_PREFIX}/{path.stem}"}
            for seq, path in segments
        ]
    }


def _register_theater_tts(conv_id: str, msg_id: str, streamer: TTSStreamer):
    if conv_id in _deleted_theater_conversations:
        streamer.cancel()
        return False
    _active_theater_tts[msg_id] = (conv_id, streamer)
    return True


def _unregister_theater_tts(msg_id: str, streamer: TTSStreamer):
    registered = _active_theater_tts.get(msg_id)
    if registered and registered[1] is streamer:
        _active_theater_tts.pop(msg_id, None)


def _cancel_theater_tts_message(msg_id: str):
    registered = _active_theater_tts.pop(msg_id, None)
    if registered:
        registered[1].cancel()


def _cancel_theater_tts_for_conversation(conv_id: str):
    message_ids = [
        msg_id for msg_id, (active_conv_id, _) in _active_theater_tts.items()
        if active_conv_id == conv_id
    ]
    for msg_id in message_ids:
        _cancel_theater_tts_message(msg_id)


async def _flush_and_cleanup_theater_tts(tts_streamer: TTSStreamer, msg_id: str):
    """Wait for every late writer, then remove audio if its message was deleted."""
    try:
        await tts_streamer.flush(wait_for_merge=True)
    finally:
        async with get_db() as db:
            cur = await db.execute("SELECT 1 FROM theater_messages WHERE id=?", (msg_id,))
            message_exists = await cur.fetchone() is not None
        if not message_exists:
            await asyncio.to_thread(
                delete_message_audio_files, [msg_id], THEATER_TTS_CACHE_DIR
            )


async def _persist_theater_assistant_message(
    conv_id: str,
    msg_id: str,
    content: str,
    created_at: float,
    reasoning_content: str = "",
) -> bool:
    """Atomically persist a late reply only while its conversation still exists."""
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO theater_messages "
            "(id, conv_id, role, content, created_at, attachments, reasoning_content) "
            "SELECT ?,?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM theater_conversations WHERE id=?)",
            (
                msg_id,
                conv_id,
                "assistant",
                content,
                created_at,
                "[]",
                reasoning_content,
                conv_id,
            ),
        )
        persisted = cur.rowcount == 1
        if persisted:
            await db.execute(
                "UPDATE theater_conversations SET updated_at=? WHERE id=?",
                (created_at, conv_id),
            )
        await db.commit()
    return persisted


class _StreamingReasoningMeta(dict):
    """Mirror reasoning_content assignments into the theater SSE queue."""

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self._queue = queue

    def __setitem__(self, key, value):
        previous = str(self.get(key) or "") if key == "reasoning_content" else ""
        super().__setitem__(key, value)
        if key != "reasoning_content":
            return
        current = str(value or "")
        delta = current[len(previous):] if current.startswith(previous) else current
        if delta:
            self._queue.put_nowait({"type": "reasoning", "content": delta})

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


class TheaterRegenerateRequest(BaseModel):
    message_id: str
    model: str
    persona_id: str = ""


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
    _deleted_theater_conversations.discard(conv_id)
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
    _deleted_theater_conversations.add(conv_id)
    _cancel_theater_tts_for_conversation(conv_id)
    message_ids = []
    try:
        async with get_db() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cur = await db.execute("SELECT id FROM theater_messages WHERE conv_id=?", (conv_id,))
            message_ids = [row[0] for row in await cur.fetchall()]
            await db.execute("DELETE FROM theater_conversations WHERE id=?", (conv_id,))
            await db.commit()
    except Exception:
        _deleted_theater_conversations.discard(conv_id)
        raise
    await asyncio.to_thread(
        delete_message_audio_files, message_ids, THEATER_TTS_CACHE_DIR
    )
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
    _cancel_theater_tts_message(msg_id)
    deleted_conv_id = None
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT conv_id FROM theater_messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if row:
            deleted_conv_id = row["conv_id"]
            await db.execute("DELETE FROM theater_messages WHERE id=?", (msg_id,))
            await db.commit()
    if deleted_conv_id:
        await asyncio.to_thread(
            delete_message_audio_files, [msg_id], THEATER_TTS_CACHE_DIR
        )
        await manager.broadcast({"type": "theater_msg_deleted", "data": {"id": msg_id, "conv_id": deleted_conv_id}})
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
    _q: asyncio.Queue = asyncio.Queue()
    usage_meta: dict = _StreamingReasoningMeta(_q)

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
            event_data={"conv_id": conv_id},
            max_segments=140,
        )
        _register_theater_tts(conv_id, ai_msg_id, tts_streamer)

    async def _bg_generate():
        full_text = ""
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            async def content_stream():
                async for chunk in stream_ai(history, model_key, usage_meta, temperature=temperature):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    yield chunk

            stream_result = await _consume_theater_stream(
                content_stream(),
                _q,
                tts_streamer,
            )
            full_text = stream_result.committed_text
            if stream_result.notice:
                full_text = f"{full_text}\n\n[{stream_result.notice}]"

            full_text = full_text.strip()

            now2 = time.time()
            reasoning_content = str(usage_meta.get("reasoning_content") or "").strip()
            message_persisted = await _persist_theater_assistant_message(
                conv_id, ai_msg_id, full_text, now2, reasoning_content
            )

            if not message_persisted:
                return

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                      "content": full_text, "created_at": now2, "attachments": [],
                      "reasoning_content": reasoning_content}
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
                    await _flush_and_cleanup_theater_tts(tts_streamer, ai_msg_id)
                except Exception:
                    pass
                finally:
                    _unregister_theater_tts(ai_msg_id, tts_streamer)
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
async def regenerate_message(conv_id: str, body: TheaterRegenerateRequest,
                             context_limit: int = 20, temperature: Optional[float] = None,
                             tts_enabled: bool = False, tts_voice: str = ""):
    async with get_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        cur = await db.execute("SELECT model, persona_id FROM theater_conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="theater conversation not found")

        cur = await db.execute(
            "SELECT role FROM theater_messages WHERE id=? AND conv_id=?",
            (body.message_id, conv_id),
        )
        target = await cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="theater message not found")
        if target["role"] != "assistant":
            raise HTTPException(status_code=400, detail="only AI messages can be regenerated")

        model_key = body.model.strip() or conv["model"] or DEFAULT_MODEL
        persona_id = body.persona_id
        now = time.time()
        await db.execute("DELETE FROM theater_messages WHERE id=?", (body.message_id,))
        await db.execute(
            "UPDATE theater_conversations SET model=?, persona_id=?, updated_at=? WHERE id=?",
            (model_key, persona_id, now, conv_id),
        )
        await db.commit()

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

    _cancel_theater_tts_message(body.message_id)
    await asyncio.to_thread(
        delete_message_audio_files, [body.message_id], THEATER_TTS_CACHE_DIR
    )
    await manager.broadcast({
        "type": "theater_msg_deleted",
        "data": {"id": body.message_id, "conv_id": conv_id},
    })

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
    _q: asyncio.Queue = asyncio.Queue()
    usage_meta: dict = _StreamingReasoningMeta(_q)

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
            event_data={"conv_id": conv_id},
            max_segments=140,
        )
        _register_theater_tts(conv_id, ai_msg_id, tts_streamer)

    async def _bg_generate():
        full_text = ""
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            async def content_stream():
                async for chunk in stream_ai(history, model_key, usage_meta, temperature=temperature):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    yield chunk

            stream_result = await _consume_theater_stream(
                content_stream(),
                _q,
                tts_streamer,
            )
            full_text = stream_result.committed_text
            if stream_result.notice:
                full_text = f"{full_text}\n\n[{stream_result.notice}]"

            full_text = full_text.strip()

            now2 = time.time()
            reasoning_content = str(usage_meta.get("reasoning_content") or "").strip()
            message_persisted = await _persist_theater_assistant_message(
                conv_id, ai_msg_id, full_text, now2, reasoning_content
            )

            if not message_persisted:
                return

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                      "content": full_text, "created_at": now2, "attachments": [],
                      "reasoning_content": reasoning_content}
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
                    await _flush_and_cleanup_theater_tts(tts_streamer, ai_msg_id)
                except Exception:
                    pass
                finally:
                    _unregister_theater_tts(ai_msg_id, tts_streamer)
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
