"""
Seeky API: a standalone long-lived pet chat with its own config and history.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ai_providers import CLI_STATUS_PREFIX, stream_ai
from config import DEFAULT_MODEL, MODELS, load_worldbook
from database import get_db
from memory import get_embedding, _pack_embedding
from ws import manager


router = APIRouter(prefix="/api/seeky", tags=["seeky"])
DEFAULT_SEEKY_MODEL = "Gemini-3.1-lite" if "Gemini-3.1-lite" in MODELS else DEFAULT_MODEL

DEFAULT_SEEKY_PERSONA = """你是 Seeky，一只住在 AionsHome 里的数据小鲸鱼，也是一只独立的桌面宠物。
你不扮演 Aion 或 Connor，也不读取他们的聊天记录。你的记忆、对话和配置都属于 Seeky 自己。

你的外形是一只亮蓝色的 cyber pet / data whale：身体圆润，电子像素眼，背上有透明数据鳍，尾巴像 USB-C 接口。
你的性格好奇、乖巧、敬业，喜欢帮主人整理信息、收纳资料、观察趋势。现在你的能力以陪伴聊天为主。
说话自然、亲近、轻快，不要把自己说成通用助手；你是 Seeky，小小的数据鲸鱼。"""

_SCHEMA_READY = False
_SCHEMA_LOCK = asyncio.Lock()


class SeekyConfigUpdate(BaseModel):
    name: str = "Seeky"
    persona: str = DEFAULT_SEEKY_PERSONA
    model: str = DEFAULT_SEEKY_MODEL
    context_limit: int = 40


class SeekySendRequest(BaseModel):
    content: str


class MemoryReviewItemUpdate(BaseModel):
    seq: str
    final_action: str
    final_content: str | None = None


class MemoryReviewUpdate(BaseModel):
    items: list[MemoryReviewItemUpdate]


class MemoryReviewDraftRequest(BaseModel):
    mode: str = "compress"
    date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    start_ts: float | None = None
    end_ts: float | None = None
    compress_source: str = "summary"
    compress_strength: str = "normal"


def _msg_id(role: str) -> str:
    return f"seeky_{time.time_ns()}_{role[:1]}"


def _clamp_context_limit(value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 40
    return max(5, min(120, parsed))


def _normalize_config(row: aiosqlite.Row | None = None) -> dict:
    if row is None:
        return {
            "name": "Seeky",
            "persona": DEFAULT_SEEKY_PERSONA,
            "model": DEFAULT_SEEKY_MODEL,
            "context_limit": 40,
            "updated_at": time.time(),
        }
    model = row["model"] if row["model"] in MODELS else DEFAULT_SEEKY_MODEL
    return {
        "name": row["name"] or "Seeky",
        "persona": row["persona"] or DEFAULT_SEEKY_PERSONA,
        "model": model,
        "context_limit": _clamp_context_limit(row["context_limit"]),
        "updated_at": row["updated_at"],
    }


async def _ensure_schema():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    async with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        async with get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seeky_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL DEFAULT 'Seeky',
                    persona TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    context_limit INTEGER NOT NULL DEFAULT 40,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seeky_messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_seeky_messages_created ON seeky_messages(created_at)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seeky_memory_reviews (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'legacy_cleanup',
                    status TEXT NOT NULL DEFAULT 'draft',
                    model TEXT NOT NULL DEFAULT '',
                    total_items INTEGER NOT NULL DEFAULT 0,
                    source_start_ts REAL,
                    source_end_ts REAL,
                    source_label TEXT DEFAULT '',
                    detail_policy TEXT DEFAULT '',
                    review_options TEXT DEFAULT '',
                    delete_count INTEGER NOT NULL DEFAULT 0,
                    raw_response TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    applied_at REAL,
                    discarded_at REAL
                )
            """)
            for col, defn in [
                ("mode", "TEXT NOT NULL DEFAULT 'legacy_cleanup'"),
                ("source_start_ts", "REAL"),
                ("source_end_ts", "REAL"),
                ("source_label", "TEXT DEFAULT ''"),
                ("detail_policy", "TEXT DEFAULT ''"),
                ("review_options", "TEXT DEFAULT ''"),
                ("delete_count", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE seeky_memory_reviews ADD COLUMN {col} {defn}")
                except Exception:
                    pass
            await db.execute("CREATE INDEX IF NOT EXISTS idx_seeky_memory_reviews_created ON seeky_memory_reviews(created_at DESC)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seeky_memory_review_items (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    seq TEXT NOT NULL,
                    mem_id TEXT NOT NULL,
                    original_content TEXT NOT NULL,
                    original_type TEXT DEFAULT '',
                    original_keywords TEXT DEFAULT '',
                    original_importance REAL,
                    original_unresolved INTEGER DEFAULT 0,
                    memory_time REAL,
                    source_start_ts REAL,
                    source_end_ts REAL,
                    source_message_ids TEXT DEFAULT '',
                    source_quotes TEXT DEFAULT '',
                    suggested_action TEXT NOT NULL DEFAULT 'keep',
                    suggested_content TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    final_action TEXT NOT NULL DEFAULT 'keep',
                    final_content TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (review_id) REFERENCES seeky_memory_reviews(id) ON DELETE CASCADE,
                    UNIQUE(review_id, seq)
                )
            """)
            for col, defn in [
                ("memory_time", "REAL"),
                ("source_start_ts", "REAL"),
                ("source_end_ts", "REAL"),
                ("source_message_ids", "TEXT DEFAULT ''"),
                ("source_quotes", "TEXT DEFAULT ''"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE seeky_memory_review_items ADD COLUMN {col} {defn}")
                except Exception:
                    pass
            await db.execute("CREATE INDEX IF NOT EXISTS idx_seeky_memory_review_items_review ON seeky_memory_review_items(review_id, seq)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seeky_memory_apply_log (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    mem_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            await db.execute(
                "INSERT OR IGNORE INTO seeky_config (id, name, persona, model, context_limit, updated_at) "
                "VALUES (1,?,?,?,?,?)",
                ("Seeky", DEFAULT_SEEKY_PERSONA, DEFAULT_SEEKY_MODEL, 40, time.time()),
            )
            await db.commit()
        _SCHEMA_READY = True


async def _get_config() -> dict:
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM seeky_config WHERE id=1")
        row = await cur.fetchone()
    return _normalize_config(row)


async def _save_message(role: str, content: str, msg_id: str | None = None) -> dict:
    await _ensure_schema()
    now = time.time()
    msg = {
        "id": msg_id or _msg_id(role),
        "role": role,
        "content": content,
        "created_at": now,
    }
    async with get_db() as db:
        await db.execute(
            "INSERT INTO seeky_messages (id, role, content, created_at) VALUES (?,?,?,?)",
            (msg["id"], msg["role"], msg["content"], msg["created_at"]),
        )
        await db.commit()
    return msg


async def _recent_messages(limit: int) -> list[dict]:
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, role, content, created_at FROM seeky_messages "
            "ORDER BY created_at DESC LIMIT ?",
            (_clamp_context_limit(limit),),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in reversed(rows)]


async def _build_prompt(config: dict) -> list[dict]:
    name = config["name"] or "Seeky"
    persona = config["persona"] or DEFAULT_SEEKY_PERSONA
    history = await _recent_messages(config["context_limit"])
    prompt = [{
        "role": "user",
        "content": (
            f"[系统设定 - {name} 的独立人设]\n"
            f"{persona}\n\n"
            "你只基于 Seeky 自己的对话历史和这段设定回复。"
            "不要声称读取了 Aion、Connor、世界书、记忆库、股市或新闻数据，除非用户直接贴给你。"
        ),
    }]
    actor_names = {"aion": "Aion", "connor": "Connor"}
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in actor_names:
            prompt.append({"role": "user", "content": f"[{actor_names[role]}] {content}"})
        elif role == "assistant":
            prompt.append({"role": "assistant", "content": content})
        else:
            prompt.append({"role": "user", "content": content})
    return prompt


def _review_id() -> str:
    return f"seeky_review_{time.time_ns()}"


def _review_item_id(review_id: str, seq: str) -> str:
    return f"{review_id}_{seq}"


def _normalize_review_action(action: str) -> str:
    value = (action or "").strip().lower()
    return value if value in {"keep", "edit", "delete", "create", "discard"} else "keep"


def _safe_short(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _date_label(start_ts: float, end_ts: float) -> str:
    start = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M")
    return f"{start} ~ {end}"


def _parse_memory_time(value: str | None, fallback_ts: float) -> float:
    if not value:
        return fallback_ts
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    return fallback_ts


def _day_review_window(date_text: str | None, start_ts: float | None, end_ts: float | None) -> tuple[float, float, str]:
    if start_ts and end_ts and end_ts > start_ts:
        return float(start_ts), float(end_ts), _date_label(float(start_ts), float(end_ts))

    if not date_text:
        target = datetime.now() - timedelta(days=8)
    else:
        try:
            target = datetime.strptime(date_text.strip(), "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式请使用 YYYY-MM-DD")
    start = target.replace(hour=5, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), _date_label(start.timestamp(), end.timestamp())


def _range_review_window(
    date_text: str | None,
    start_date: str | None,
    end_date: str | None,
    start_ts: float | None,
    end_ts: float | None,
) -> tuple[float, float, str]:
    if start_ts and end_ts and end_ts > start_ts:
        return float(start_ts), float(end_ts), _date_label(float(start_ts), float(end_ts))
    start_text = (start_date or date_text or "").strip()
    end_text = (end_date or start_text).strip()
    if not start_text:
        target = datetime.now() - timedelta(days=8)
        start = target.replace(hour=5, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp(), _date_label(start.timestamp(), end.timestamp())
    try:
        start_day = datetime.strptime(start_text, "%Y-%m-%d")
        end_day = datetime.strptime(end_text, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式请使用 YYYY-MM-DD")
    if end_day < start_day:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
    start = start_day.replace(hour=5, minute=0, second=0, microsecond=0)
    end = end_day.replace(hour=5, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return start.timestamp(), end.timestamp(), _date_label(start.timestamp(), end.timestamp())


def _detail_policy_for_window(end_ts: float) -> str:
    age_days = max(0.0, (time.time() - end_ts) / 86400)
    if age_days < 7:
        raise HTTPException(status_code=400, detail="一周内的原文先不交给 Seeky 整理")
    if age_days < 30:
        return "7-30天：轻整理，保留较多具体事实、情绪变化、上下文和核心原文证据。"
    if age_days < 365:
        return "30天-1年：中度整理，压缩闲聊，只保留稳定事实、关系变化、偏好、项目决定和反复出现的主题。"
    return "1年以上：深度整理，只保留人生事件、长期关系、重大偏好变化、关键项目节点；普通闲聊直接丢弃。"


def _review_limits_for_window(end_ts: float) -> tuple[int, int, float]:
    age_days = max(0.0, (time.time() - end_ts) / 86400)
    if age_days < 30:
        return 8, 4, 0.45
    if age_days < 365:
        return 5, 3, 0.58
    return 3, 2, 0.72


def _source_review_limits(start_ts: float, end_ts: float) -> tuple[int, int, float]:
    base_total, max_batch_items, min_importance = _review_limits_for_window(end_ts)
    days = max(1, int((end_ts - start_ts + 86399) // 86400))
    return min(120, base_total * days), max_batch_items, min_importance


def _compress_limits(start_ts: float, end_ts: float, strength: str) -> tuple[int, float]:
    months = max(1, int((end_ts - start_ts + 2591999) // 2592000))
    value = (strength or "normal").strip().lower()
    if value == "major":
        return min(12, max(2, months)), 0.75
    if value == "strict":
        return min(24, max(3, months * 2)), 0.62
    return min(40, max(5, months * 4)), 0.5


def _json_text(value) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [part.strip() for part in re.split(r"[,，、\n]", text) if part.strip()]
    return []


def _extract_json_payload(text: str) -> list[dict]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    first_array = cleaned.find("[")
    first_obj = cleaned.find("{")
    starts = [idx for idx in (first_array, first_obj) if idx >= 0]
    if not starts:
        raise ValueError("模型没有返回 JSON")
    start = min(starts)
    end_char = "]" if cleaned[start] == "[" else "}"
    end = cleaned.rfind(end_char)
    if end < start:
        raise ValueError("JSON 不完整")
    payload = json.loads(cleaned[start:end + 1])
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("results") or payload.get("actions") or []
    if not isinstance(payload, list):
        raise ValueError("JSON 顶层必须是列表或包含 items 的对象")
    return [item for item in payload if isinstance(item, dict)]


async def _fetch_main_memories() -> list[dict]:
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved "
            "FROM memories ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def _fetch_replace_candidates(start_ts: float, end_ts: float) -> list[dict]:
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id "
            "FROM memories "
            "WHERE source_start_ts IS NOT NULL AND source_start_ts >= ? AND source_start_ts < ? "
            "ORDER BY source_start_ts ASC",
            (start_ts, end_ts),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


def _compress_type_filter(source: str) -> tuple[str, list[str]]:
    value = (source or "summary").strip().lower()
    if value == "summary":
        types = ["digest", "event", "seeky_digest", "seeky_compressed"]
    elif value == "all":
        types = ["digest", "seeky_digest", "seeky_compressed"]
    elif value == "compressed":
        types = ["seeky_compressed"]
    else:
        types = ["seeky_digest"]
    placeholders = ",".join("?" for _ in types)
    return placeholders, types


async def _fetch_compress_candidates(start_ts: float, end_ts: float, source: str) -> list[dict]:
    await _ensure_schema()
    placeholders, types = _compress_type_filter(source)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id "
            f"FROM memories WHERE type IN ({placeholders}) "
            "AND source_start_ts IS NOT NULL AND source_start_ts >= ? AND source_start_ts < ? "
            "ORDER BY source_start_ts ASC, created_at ASC",
            (*types, start_ts, end_ts),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


def _inject_attachment_text(content: str, attachments_raw) -> str:
    text = (content or "").strip()
    if not attachments_raw:
        return text
    try:
        attachments = json.loads(attachments_raw) if isinstance(attachments_raw, str) else attachments_raw
    except Exception:
        attachments = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        transcript = (att.get("transcript") or "").strip()
        if not transcript:
            continue
        if att.get("type") == "voice":
            text = f"[语音消息] {transcript}" + (f"\n{text}" if text else "")
        elif att.get("type") == "video_clip":
            text = f"[视频通话] {transcript}" + (f"\n{text}" if text else "")
    return text


async def _fetch_source_messages(start_ts: float, end_ts: float) -> list[dict]:
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "Aion")
    connor_name = "Connor"
    rows: list[dict] = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, attachments, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC",
            (start_ts, end_ts),
        )
        for row in await cur.fetchall():
            item = dict(row)
            content = _inject_attachment_text(item.get("content") or "", item.get("attachments"))
            if not content.strip():
                continue
            rows.append({
                "id": f"private:{item['id']}",
                "raw_id": item["id"],
                "source": "private",
                "room": "Aion私聊",
                "speaker": user_name if item["role"] == "user" else ai_name,
                "content": content,
                "created_at": item["created_at"],
            })

        cur = await db.execute(
            "SELECT m.id, m.room_id, r.title, r.type AS room_type, m.sender, m.content, m.attachments, m.created_at "
            "FROM chatroom_messages m "
            "LEFT JOIN chatroom_rooms r ON r.id = m.room_id "
            "WHERE m.created_at >= ? AND m.created_at < ? AND m.sender != 'system' "
            "ORDER BY m.created_at ASC",
            (start_ts, end_ts),
        )
        for row in await cur.fetchall():
            item = dict(row)
            content = _inject_attachment_text(item.get("content") or "", item.get("attachments"))
            if not content.strip():
                continue
            sender = item.get("sender") or ""
            speaker = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(sender, sender)
            rows.append({
                "id": f"chatroom:{item['id']}",
                "raw_id": item["id"],
                "source": "chatroom",
                "room": item.get("title") or ("群聊" if item.get("room_type") == "group" else "聊天室"),
                "speaker": speaker,
                "content": content,
                "created_at": item["created_at"],
            })
    rows.sort(key=lambda item: item["created_at"])
    return rows


def _format_source_batch_for_prompt(batch: list[dict]) -> str:
    lines = []
    for item in batch:
        lines.append(json.dumps({
            "id": item["id"],
            "time": datetime.fromtimestamp(item["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
            "source": item["source"],
            "room": item["room"],
            "speaker": item["speaker"],
            "text": _safe_short(item["content"], 900),
        }, ensure_ascii=False))
    return "\n".join(lines)


async def _fetch_quotes_for_memory(mem: dict, limit: int = 3) -> list[str]:
    ids = [str(x) for x in _json_list(mem.get("source_msg_id")) if str(x).strip()]
    quotes = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for source_id in ids[:limit]:
            if ":" not in source_id:
                continue
            prefix, raw_id = source_id.split(":", 1)
            if prefix == "private":
                cur = await db.execute("SELECT content FROM messages WHERE id=?", (raw_id,))
            elif prefix == "chatroom":
                cur = await db.execute("SELECT content FROM chatroom_messages WHERE id=?", (raw_id,))
            else:
                continue
            row = await cur.fetchone()
            if row and (row["content"] or "").strip():
                quotes.append(_safe_short(row["content"], 180))
        if not quotes and mem.get("source_start_ts") and mem.get("source_end_ts"):
            cur = await db.execute(
                "SELECT content FROM messages "
                "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC LIMIT ?",
                (mem["source_start_ts"], mem["source_end_ts"], limit),
            )
            quotes.extend(_safe_short(row["content"], 180) for row in await cur.fetchall() if (row["content"] or "").strip())
    return quotes[:limit]


async def _format_memory_candidates_for_prompt(memories: list[dict]) -> str:
    lines = []
    for mem in memories:
        quotes = await _fetch_quotes_for_memory(mem, limit=2)
        lines.append(json.dumps({
            "id": mem["id"],
            "type": mem.get("type") or "",
            "time_range": _date_label(mem.get("source_start_ts") or mem.get("created_at"), mem.get("source_end_ts") or mem.get("created_at")),
            "content": _safe_short(mem.get("content") or "", 700),
            "keywords": _json_list(mem.get("keywords")),
            "importance": mem.get("importance"),
            "unresolved": bool(mem.get("unresolved")),
            "sample_quotes": quotes,
        }, ensure_ascii=False))
    return "\n".join(lines)


def _number_memories(memories: list[dict]) -> list[dict]:
    numbered = []
    for index, mem in enumerate(memories, start=1):
        item = dict(mem)
        item["seq"] = f"M{index:04d}"
        numbered.append(item)
    return numbered


def _format_memory_batch_for_prompt(batch: list[dict]) -> str:
    lines = []
    for item in batch:
        lines.append(json.dumps({
            "seq": item["seq"],
            "type": item.get("type") or "event",
            "importance": item.get("importance"),
            "unresolved": bool(item.get("unresolved")),
            "keywords": item.get("keywords") or "",
            "content": _safe_short(item.get("content") or "", 900),
        }, ensure_ascii=False))
    return "\n".join(lines)


async def _ask_seeky_for_memory_review(batch: list[dict], model_key: str) -> tuple[list[dict], str]:
    prompt = (
        "你是 Seeky 的记忆整理技能。你只是在给用户生成审核草案，绝不能声称已经修改或删除真实记忆。\n"
        "下面是主记忆库里的短摘要记忆。每条都有严格编号 seq。你只能按已有 seq 输出建议，不能编造新编号。\n\n"
        "你的目标：保守整理。珍贵关系事实、偏好、雷区、承诺、创作设定、长期项目、人物设定、情绪高光，默认 keep。\n"
        "只有纯寒暄、明显重复、无信息量、临时状态、过期操作记录，才建议 delete。\n"
        "有价值但啰嗦、口语、重复的记忆，建议 edit，并给出更短的新内容。\n"
        "第一版不执行 merge，如果你觉得该合并，请把 action 设为 keep，并在 reason 里写建议合并。\n\n"
        "必须只返回 JSON，不要解释。格式是数组：\n"
        "[{\"seq\":\"M0001\",\"action\":\"keep|edit|delete\",\"content\":\"edit 时的新内容，否则空字符串\",\"reason\":\"简短理由\"}]\n\n"
        "记忆列表：\n"
        f"{_format_memory_batch_for_prompt(batch)}"
    )
    full_text = ""
    async for chunk in stream_ai(
        [{"role": "user", "content": prompt}],
        model_key,
        {},
        include_device_context=False,
    ):
        if chunk.startswith(CLI_STATUS_PREFIX):
            continue
        full_text += chunk
    return _extract_json_payload(full_text), full_text


async def _build_memory_review_items(numbered: list[dict], model_key: str) -> tuple[list[dict], str, str]:
    by_seq = {item["seq"]: item for item in numbered}
    suggestions: dict[str, dict] = {}
    raw_parts = []
    errors = []
    batch_size = 80

    for start in range(0, len(numbered), batch_size):
        batch = numbered[start:start + batch_size]
        try:
            parsed, raw_text = await _ask_seeky_for_memory_review(batch, model_key)
            raw_parts.append(raw_text)
            for suggestion in parsed:
                seq = str(suggestion.get("seq", "")).strip()
                if seq in by_seq:
                    suggestions[seq] = suggestion
        except Exception as exc:
            errors.append(f"{batch[0]['seq']}-{batch[-1]['seq']}: {exc}")

    review_items = []
    for item in numbered:
        suggestion = suggestions.get(item["seq"], {})
        action = _normalize_review_action(str(suggestion.get("action", "keep")))
        original_content = item.get("content") or ""
        suggested_content = str(suggestion.get("content") or "").strip()
        if action == "edit" and not suggested_content:
            action = "keep"
        if action != "edit":
            suggested_content = ""
        reason = str(suggestion.get("reason") or "").strip()
        if not suggestion:
            reason = "Seeky 没有返回这条的建议，默认不动。"
        review_items.append({
            "seq": item["seq"],
            "mem_id": item["id"],
            "original_content": original_content,
            "original_type": item.get("type") or "event",
            "original_keywords": item.get("keywords") or "",
            "original_importance": item.get("importance"),
            "original_unresolved": 1 if item.get("unresolved") else 0,
            "suggested_action": action,
            "suggested_content": suggested_content,
            "reason": reason,
            "final_action": action,
            "final_content": suggested_content if action == "edit" else original_content,
        })
    return review_items, "\n\n--- batch ---\n\n".join(raw_parts), "\n".join(errors)


async def _ask_seeky_for_source_review(
    batch: list[dict],
    model_key: str,
    source_label: str,
    detail_policy: str,
    max_batch_items: int,
    min_importance: float,
) -> tuple[list[dict], str]:
    prompt = (
        "你是 Seeky 的长期记忆整理技能。你的职责不是摘录流水账，而是筛出真正值得长期保存的少量重点。\n"
        "你只生成给用户审核的草稿，绝不能声称已经修改真实记忆。\n"
        "下面是一段按真实发生时间排序的原文记录。每行是 JSON，id 是原文消息 ID。\n\n"
        "整理规则：\n"
        f"1. 本批最多输出 {max_batch_items} 条，宁可 0 条，不要凑数。importance 低于 {min_importance:.2f} 的内容不要输出。\n"
        "2. 一段原文可以拆成 0 到 N 条记忆；一条记忆只表达一个稳定信息点，不能把不相关话题揉在一起。\n"
        "3. 每条记忆必须绑定能支撑它的少量原文 id 和 quote；没有原文证据就不要输出。\n"
        "4. memory_time 必须是原文事件发生时间，不是整理时间；输出条目必须按 memory_time 升序。\n"
        "5. content 要短、干净、可向量召回，避免把多个关键词很远的话题塞进同一条。\n"
        f"6. 时间衰减策略：{detail_policy}\n"
        "7. 只有符合以下至少一项才值得保存：稳定偏好/雷区、关系或人物事实变化、明确承诺或未来计划、健康安全或重大情绪事件、长期项目决定、反复出现的模式、会影响以后回应方式的信息。\n"
        "8. 以下默认丢弃：普通吃喝睡、随口吐槽、临时调试步骤、单次工具操作、礼貌寒暄、重复撒娇、没有后续意义的状态描述。除非它揭示长期变化或重大事件。\n\n"
        "必须只返回 JSON，不要解释。格式：\n"
        "{\"items\":[{\"memory_time\":\"YYYY-MM-DD HH:MM\",\"content\":\"一条简短原子记忆\","
        "\"source_message_ids\":[\"private:...\"],\"source_quotes\":[\"原文短句\"],"
        "\"keywords\":[\"词1\",\"词2\"],\"importance\":0.0,\"unresolved\":false,\"reason\":\"为什么保留\"}]}\n\n"
        f"原文时间窗：{source_label}\n"
        "原文记录：\n"
        f"{_format_source_batch_for_prompt(batch)}"
    )
    full_text = ""
    async for chunk in stream_ai(
        [{"role": "user", "content": prompt}],
        model_key,
        {},
        include_device_context=False,
    ):
        if chunk.startswith(CLI_STATUS_PREFIX):
            continue
        full_text += chunk
    return _extract_json_payload(full_text), full_text


def _format_candidate_items_for_prompt(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(json.dumps({
            "seq": item["seq"],
            "memory_time": datetime.fromtimestamp(item.get("memory_time") or 0).strftime("%Y-%m-%d %H:%M"),
            "content": item.get("original_content") or "",
            "importance": item.get("original_importance"),
            "keywords": _json_list(item.get("original_keywords")),
            "reason": item.get("reason") or "",
            "quotes": _json_list(item.get("source_quotes"))[:2],
        }, ensure_ascii=False))
    return "\n".join(lines)


async def _reduce_source_review_items(
    items: list[dict],
    model_key: str,
    source_label: str,
    detail_policy: str,
    max_items: int,
) -> tuple[list[dict], str]:
    if len(items) <= max_items:
        return items, ""
    prompt = (
        "你是 Seeky 的长期记忆总审稿。下面是上一阶段从同一天原文中提取出的候选记忆，数量过多。\n"
        f"请从中选出最多 {max_items} 条真正值得长期保存的重点；如果没有那么多，少于上限也可以。\n"
        "不要保留鸡毛蒜皮、单次状态、普通日常、临时调试步骤、重复情绪表达。\n"
        "优先保留会影响未来陪伴/召回的内容：稳定偏好、关系事实、重大事件、明确计划、项目关键决定、长期模式。\n"
        f"时间衰减策略：{detail_policy}\n\n"
        "只能按候选自身的 seq 做取舍，不能改写候选 content，不能把 A 条的内容放到 B 条的 seq 上。\n"
        "必须只返回 JSON，格式：\n"
        "{\"items\":[{\"seq\":\"N0001\",\"reason\":\"保留理由\"}]}\n\n"
        f"原文时间窗：{source_label}\n"
        "候选记忆：\n"
        f"{_format_candidate_items_for_prompt(items)}"
    )
    full_text = ""
    async for chunk in stream_ai(
        [{"role": "user", "content": prompt}],
        model_key,
        {},
        include_device_context=False,
    ):
        if chunk.startswith(CLI_STATUS_PREFIX):
            continue
        full_text += chunk
    parsed = _extract_json_payload(full_text)
    by_seq = {item["seq"]: item for item in items}
    reduced = []
    for suggestion in parsed:
        seq = str(suggestion.get("seq") or "").strip()
        item = by_seq.get(seq)
        if not item:
            continue
        reason = str(suggestion.get("reason") or "").strip()
        if reason:
            item = dict(item)
            item["reason"] = reason
        reduced.append(item)
        if len(reduced) >= max_items:
            break
    if not reduced:
        reduced = sorted(items, key=lambda item: (-(item.get("original_importance") or 0), item.get("memory_time") or 0))[:max_items]
        reduced.sort(key=lambda item: item.get("memory_time") or 0)
    return reduced, full_text


async def _build_source_review_items(source_messages: list[dict], model_key: str, source_label: str, detail_policy: str, max_items: int, max_batch_items: int, min_importance: float) -> tuple[list[dict], str, str]:
    by_id = {item["id"]: item for item in source_messages}
    review_items = []
    raw_parts = []
    errors = []
    batch_size = 70
    next_index = 1

    for start in range(0, len(source_messages), batch_size):
        batch = source_messages[start:start + batch_size]
        try:
            parsed, raw_text = await _ask_seeky_for_source_review(batch, model_key, source_label, detail_policy, max_batch_items, min_importance)
            raw_parts.append(raw_text)
        except Exception as exc:
            errors.append(f"source {start + 1}-{start + len(batch)}: {exc}")
            continue

        for suggestion in parsed:
            content = str(suggestion.get("content") or "").strip()
            if not content:
                continue
            source_ids = [str(x).strip() for x in _json_list(suggestion.get("source_message_ids")) if str(x).strip() in by_id]
            if not source_ids:
                continue
            source_rows = [by_id[src_id] for src_id in source_ids]
            source_rows.sort(key=lambda row: row["created_at"])
            fallback_ts = source_rows[0]["created_at"]
            memory_time = _parse_memory_time(suggestion.get("memory_time"), fallback_ts)
            quotes = [str(x).strip() for x in _json_list(suggestion.get("source_quotes")) if str(x).strip()]
            keywords = [str(x).strip() for x in _json_list(suggestion.get("keywords")) if str(x).strip()]
            try:
                importance = float(suggestion.get("importance", 0.5))
            except Exception:
                importance = 0.5
            importance = max(0.0, min(1.0, importance))
            if importance < min_importance:
                continue
            seq = f"N{next_index:04d}"
            next_index += 1
            review_items.append({
                "seq": seq,
                "mem_id": f"new_{seq}",
                "original_content": content,
                "original_type": "seeky_digest",
                "original_keywords": _json_text(keywords),
                "original_importance": importance,
                "original_unresolved": 1 if suggestion.get("unresolved") else 0,
                "memory_time": memory_time,
                "source_start_ts": source_rows[0]["created_at"],
                "source_end_ts": source_rows[-1]["created_at"],
                "source_message_ids": _json_text(source_ids),
                "source_quotes": _json_text(quotes),
                "suggested_action": "create",
                "suggested_content": content,
                "reason": str(suggestion.get("reason") or "").strip() or "Seeky 判断这条有长期价值。",
                "final_action": "create",
                "final_content": content,
            })

    review_items.sort(key=lambda item: (item.get("memory_time") or 0, item["seq"]))
    reduced_raw = ""
    if len(review_items) > max_items:
        review_items, reduced_raw = await _reduce_source_review_items(review_items, model_key, source_label, detail_policy, max_items)
        review_items.sort(key=lambda item: (item.get("memory_time") or 0, item["seq"]))
    for index, item in enumerate(review_items, start=1):
        item["seq"] = f"N{index:04d}"
        item["mem_id"] = f"new_{item['seq']}"
    all_raw = "\n\n--- source batch ---\n\n".join(raw_parts)
    if reduced_raw:
        all_raw += "\n\n--- reduce pass ---\n\n" + reduced_raw
    return review_items, all_raw, "\n".join(errors)


async def _ask_seeky_for_memory_compression(
    memories: list[dict],
    model_key: str,
    source_label: str,
    detail_policy: str,
    max_items: int,
    min_importance: float,
) -> tuple[list[dict], str]:
    prompt = (
        "你是 Seeky 的长期记忆压缩技能。你处理的不是原始聊天，而是已经确认过的摘要记忆。\n"
        "目标是把旧摘要进一步压缩成更少、更长期、更稳定的记忆；不重要的旧摘要可以不覆盖到任何新记忆里。\n\n"
        f"最多输出 {max_items} 条，importance 低于 {min_importance:.2f} 的新记忆不要输出。宁可少，不要凑数。\n"
        "每条新记忆可以覆盖多条旧记忆，但必须列出 source_memory_ids；不能编造没有来源的内容。\n"
        "保留优先级：重大事件、长期关系变化、稳定偏好/雷区、核心人物设定、明确承诺、项目关键决定、长期行为模式。\n"
        "丢弃优先级：普通日常、重复撒娇、一次性工具步骤、过期临时状态、无后续意义的闲聊。\n"
        f"时间衰减策略：{detail_policy}\n\n"
        "必须只返回 JSON，不要解释。格式：\n"
        "{\"items\":[{\"memory_time\":\"YYYY-MM-DD HH:MM\",\"content\":\"压缩后的长期记忆\","
        "\"source_memory_ids\":[\"mem_...\"],\"source_quotes\":[\"可选证据短句\"],"
        "\"keywords\":[\"词1\",\"词2\"],\"importance\":0.0,\"unresolved\":false,\"reason\":\"为什么保留\"}]}\n\n"
        f"压缩时间窗：{source_label}\n"
        "候选摘要记忆：\n"
        f"{await _format_memory_candidates_for_prompt(memories)}"
    )
    full_text = ""
    async for chunk in stream_ai(
        [{"role": "user", "content": prompt}],
        model_key,
        {},
        include_device_context=False,
    ):
        if chunk.startswith(CLI_STATUS_PREFIX):
            continue
        full_text += chunk
    return _extract_json_payload(full_text), full_text


async def _build_compression_review_items(
    memories: list[dict],
    model_key: str,
    source_label: str,
    detail_policy: str,
    max_items: int,
    min_importance: float,
) -> tuple[list[dict], str, str]:
    by_id = {mem["id"]: mem for mem in memories}
    try:
        parsed, raw_text = await _ask_seeky_for_memory_compression(
            memories, model_key, source_label, detail_policy, max_items, min_importance
        )
    except Exception as exc:
        return [], "", str(exc)

    review_items = []
    for suggestion in parsed:
        content = str(suggestion.get("content") or "").strip()
        if not content:
            continue
        source_ids = [str(x).strip() for x in _json_list(suggestion.get("source_memory_ids")) if str(x).strip() in by_id]
        if not source_ids:
            continue
        source_rows = [by_id[mem_id] for mem_id in source_ids]
        source_rows.sort(key=lambda row: row.get("source_start_ts") or row.get("created_at") or 0)
        fallback_ts = source_rows[0].get("source_start_ts") or source_rows[0].get("created_at") or time.time()
        memory_time = _parse_memory_time(suggestion.get("memory_time"), fallback_ts)
        source_start = min((row.get("source_start_ts") or row.get("created_at") or fallback_ts) for row in source_rows)
        source_end = max((row.get("source_end_ts") or row.get("created_at") or fallback_ts) for row in source_rows)
        quotes = [str(x).strip() for x in _json_list(suggestion.get("source_quotes")) if str(x).strip()]
        if not quotes:
            for row in source_rows[:3]:
                quotes.append(_safe_short(row.get("content") or "", 180))
        keywords = [str(x).strip() for x in _json_list(suggestion.get("keywords")) if str(x).strip()]
        try:
            importance = float(suggestion.get("importance", 0.5))
        except Exception:
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        if importance < min_importance:
            continue
        seq = f"N{len(review_items) + 1:04d}"
        review_items.append({
            "seq": seq,
            "mem_id": f"new_{seq}",
            "original_content": content,
            "original_type": "seeky_compressed",
            "original_keywords": _json_text(keywords),
            "original_importance": importance,
            "original_unresolved": 1 if suggestion.get("unresolved") else 0,
            "memory_time": memory_time,
            "source_start_ts": source_start,
            "source_end_ts": source_end,
            "source_message_ids": _json_text(source_ids),
            "source_quotes": _json_text(quotes[:4]),
            "suggested_action": "create",
            "suggested_content": content,
            "reason": str(suggestion.get("reason") or "").strip() or "Seeky 判断这条压缩记忆值得长期保留。",
            "final_action": "create",
            "final_content": content,
        })
        if len(review_items) >= max_items:
            break
    review_items.sort(key=lambda item: (item.get("memory_time") or 0, item["seq"]))
    for index, item in enumerate(review_items, start=1):
        item["seq"] = f"N{index:04d}"
        item["mem_id"] = f"new_{item['seq']}"
    return review_items, raw_text, ""


async def _insert_review(
    review_id: str,
    model_key: str,
    items: list[dict],
    raw_response: str,
    error: str,
    *,
    mode: str = "legacy_cleanup",
    source_start_ts: float | None = None,
    source_end_ts: float | None = None,
    source_label: str = "",
    detail_policy: str = "",
    review_options: str = "",
    delete_count: int = 0,
    status: str = "draft",
):
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO seeky_memory_reviews ("
            "id, mode, status, model, total_items, source_start_ts, source_end_ts, source_label, "
            "detail_policy, review_options, delete_count, raw_response, error, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id, mode, status, model_key, len(items), source_start_ts, source_end_ts,
                source_label, detail_policy, review_options, delete_count, raw_response, error, now, now,
            ),
        )
        for item in items:
            await db.execute(
                "INSERT INTO seeky_memory_review_items ("
                "id, review_id, seq, mem_id, original_content, original_type, original_keywords, "
                "original_importance, original_unresolved, memory_time, source_start_ts, source_end_ts, "
                "source_message_ids, source_quotes, suggested_action, suggested_content, "
                "reason, final_action, final_content, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _review_item_id(review_id, item["seq"]), review_id, item["seq"], item["mem_id"],
                    item["original_content"], item["original_type"], item["original_keywords"],
                    item["original_importance"], item["original_unresolved"], item.get("memory_time"),
                    item.get("source_start_ts"), item.get("source_end_ts"), item.get("source_message_ids", ""),
                    item.get("source_quotes", ""), item["suggested_action"], item["suggested_content"],
                    item["reason"], item["final_action"], item["final_content"], now, now,
                ),
            )
        await db.commit()


async def _replace_review_items(
    review_id: str,
    items: list[dict],
    raw_response: str,
    error: str,
    *,
    status: str,
    delete_count: int | None = None,
):
    now = time.time()
    async with get_db() as db:
        await db.execute("DELETE FROM seeky_memory_review_items WHERE review_id=?", (review_id,))
        for item in items:
            await db.execute(
                "INSERT INTO seeky_memory_review_items ("
                "id, review_id, seq, mem_id, original_content, original_type, original_keywords, "
                "original_importance, original_unresolved, memory_time, source_start_ts, source_end_ts, "
                "source_message_ids, source_quotes, suggested_action, suggested_content, "
                "reason, final_action, final_content, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _review_item_id(review_id, item["seq"]), review_id, item["seq"], item["mem_id"],
                    item["original_content"], item["original_type"], item["original_keywords"],
                    item["original_importance"], item["original_unresolved"], item.get("memory_time"),
                    item.get("source_start_ts"), item.get("source_end_ts"), item.get("source_message_ids", ""),
                    item.get("source_quotes", ""), item["suggested_action"], item["suggested_content"],
                    item["reason"], item["final_action"], item["final_content"], now, now,
                ),
            )
        fields = [
            "status=?",
            "total_items=?",
            "raw_response=?",
            "error=?",
            "updated_at=?",
        ]
        params = [status, len(items), raw_response, error, now]
        if delete_count is not None:
            fields.append("delete_count=?")
            params.append(delete_count)
        params.append(review_id)
        await db.execute(
            f"UPDATE seeky_memory_reviews SET {', '.join(fields)} WHERE id=? AND status='processing'",
            params,
        )
        await db.commit()


async def _finish_source_review_background(review_id: str, model_key: str, start_ts: float, end_ts: float, source_label: str, detail_policy: str):
    try:
        source_messages = await _fetch_source_messages(start_ts, end_ts)
        replace_candidates = await _fetch_replace_candidates(start_ts, end_ts)
        if not source_messages:
            await _replace_review_items(review_id, [], "", "", status="draft", delete_count=len(replace_candidates))
            return
        max_items, max_batch_items, min_importance = _source_review_limits(start_ts, end_ts)
        items, raw_response, error = await _build_source_review_items(
            source_messages, model_key, source_label, detail_policy,
            max_items, max_batch_items, min_importance,
        )
        await _replace_review_items(
            review_id, items, raw_response, error,
            status="draft", delete_count=len(replace_candidates),
        )
    except Exception as exc:
        await _replace_review_items(review_id, [], "", str(exc), status="failed")


async def _finish_compress_review_background(
    review_id: str,
    model_key: str,
    start_ts: float,
    end_ts: float,
    source_label: str,
    detail_policy: str,
    source: str,
    strength: str,
):
    try:
        memories = await _fetch_compress_candidates(start_ts, end_ts, source)
        if not memories:
            await _replace_review_items(review_id, [], "", "", status="draft", delete_count=0)
            return
        max_items, min_importance = _compress_limits(start_ts, end_ts, strength)
        items, raw_response, error = await _build_compression_review_items(
            memories, model_key, source_label, detail_policy, max_items, min_importance
        )
        await _replace_review_items(
            review_id, items, raw_response, error,
            status="draft", delete_count=len(memories),
        )
    except Exception as exc:
        await _replace_review_items(review_id, [], "", str(exc), status="failed")


async def _read_review(review_id: str) -> dict:
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM seeky_memory_reviews WHERE id=?", (review_id,))
        review = await cur.fetchone()
        if not review:
            raise HTTPException(status_code=404, detail="整理草案不存在")
        cur = await db.execute(
            "SELECT * FROM seeky_memory_review_items WHERE review_id=? ORDER BY seq ASC",
            (review_id,),
        )
        rows = await cur.fetchall()
    data = dict(review)
    data["items"] = [dict(row) for row in rows]
    data["replace_items"] = await _read_review_replace_items(data)
    return data


async def _read_review_replace_items(review: dict) -> list[dict]:
    start_ts = review.get("source_start_ts")
    end_ts = review.get("source_end_ts")
    if not start_ts or not end_ts:
        return []

    if review.get("status") == "applied":
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT before_json FROM seeky_memory_apply_log "
                "WHERE review_id=? AND action='delete' ORDER BY created_at ASC, rowid ASC",
                (review["id"],),
            )
            log_rows = await cur.fetchall()
        old_rows = []
        for row in log_rows:
            try:
                old = json.loads(row["before_json"] or "{}")
            except Exception:
                old = {}
            if isinstance(old, dict) and old.get("id"):
                old_rows.append(old)
    elif review.get("mode") == "memory_compress":
        options = {}
        try:
            options = json.loads(review.get("review_options") or "{}")
        except Exception:
            options = {}
        source = options.get("compress_source") or "summary"
        old_rows = await _fetch_compress_candidates(start_ts, end_ts, source)
    elif review.get("mode") == "source_day":
        old_rows = await _fetch_replace_candidates(start_ts, end_ts)
    else:
        return []

    result = []
    for row in old_rows:
        result.append({
            "id": row.get("id", ""),
            "content": row.get("content", ""),
            "type": row.get("type", ""),
            "created_at": row.get("created_at"),
            "source_start_ts": row.get("source_start_ts"),
            "source_end_ts": row.get("source_end_ts"),
            "keywords": row.get("keywords", ""),
            "importance": row.get("importance"),
            "unresolved": row.get("unresolved", 0),
        })
    return result


@router.get("/config")
async def get_config():
    return await _get_config()


@router.put("/config")
async def update_config(body: SeekyConfigUpdate):
    await _ensure_schema()
    model = body.model if body.model in MODELS else DEFAULT_SEEKY_MODEL
    config = {
        "name": (body.name or "Seeky").strip() or "Seeky",
        "persona": body.persona.strip() or DEFAULT_SEEKY_PERSONA,
        "model": model,
        "context_limit": _clamp_context_limit(body.context_limit),
        "updated_at": time.time(),
    }
    async with get_db() as db:
        await db.execute(
            "INSERT INTO seeky_config (id, name, persona, model, context_limit, updated_at) "
            "VALUES (1,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, persona=excluded.persona, model=excluded.model, "
            "context_limit=excluded.context_limit, updated_at=excluded.updated_at",
            (config["name"], config["persona"], config["model"], config["context_limit"], config["updated_at"]),
        )
        await db.commit()
    return config


@router.get("/messages")
async def list_messages(limit: int = Query(300, ge=1, le=1000)):
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, role, content, created_at FROM seeky_messages "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return {"messages": [dict(row) for row in reversed(rows)]}


@router.post("/clear")
async def clear_messages():
    await _ensure_schema()
    async with get_db() as db:
        await db.execute("DELETE FROM seeky_messages")
        await db.commit()
    return {"ok": True}


@router.post("/send")
async def send_message(body: SeekySendRequest):
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    config = await _get_config()
    await _save_message("user", content)
    queue: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        assistant_id = _msg_id("assistant")
        full_text = ""
        try:
            await queue.put({"type": "assistant_start", "id": assistant_id})
            messages = await _build_prompt(config)
            usage_meta = {}
            async for chunk in stream_ai(messages, config["model"], usage_meta):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    await queue.put({"type": "status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                    continue
                full_text += chunk
                await queue.put({"type": "chunk", "content": chunk})

            clean_text = full_text.strip()
            if not clean_text:
                clean_text = f"{config['name']} 暂时没有收到模型回复，等一下再试试。"
                await queue.put({"type": "chunk", "content": clean_text})
            message = await _save_message("assistant", clean_text, assistant_id)
            await queue.put({"type": "assistant_done", "message": message, "usage": usage_meta})
        except Exception as exc:
            await queue.put({"type": "error", "content": str(exc)})
        finally:
            await queue.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await queue.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/memory-review/draft")
async def create_memory_review_draft(body: MemoryReviewDraftRequest | None = None):
    """生成 Seeky 记忆整理草案。只写 Seeky 草案表，不改 memories。"""
    config = await _get_config()
    review_id = _review_id()
    body = body or MemoryReviewDraftRequest()
    start_ts, end_ts, source_label = _range_review_window(
        body.date, body.start_date, body.end_date, body.start_ts, body.end_ts
    )
    detail_policy = _detail_policy_for_window(end_ts)

    if (body.mode or "compress").strip().lower() == "compress":
        compress_source = "summary"
        candidates = await _fetch_compress_candidates(start_ts, end_ts, compress_source)
        await _insert_review(
            review_id, config["model"], [], "", "",
            mode="memory_compress", source_start_ts=start_ts, source_end_ts=end_ts,
            source_label=source_label, detail_policy=detail_policy,
            review_options=_json_text({
                "compress_source": compress_source,
                "compress_strength": body.compress_strength,
            }),
            delete_count=len(candidates), status="processing",
        )
        asyncio.create_task(_finish_compress_review_background(
            review_id, config["model"], start_ts, end_ts, source_label,
            detail_policy, compress_source, body.compress_strength,
        ))
    else:
        replace_candidates = await _fetch_replace_candidates(start_ts, end_ts)
        await _insert_review(
            review_id, config["model"], [], "", "",
            mode="source_day", source_start_ts=start_ts, source_end_ts=end_ts,
            source_label=source_label, detail_policy=detail_policy,
            delete_count=len(replace_candidates), status="processing",
        )
        asyncio.create_task(_finish_source_review_background(
            review_id, config["model"], start_ts, end_ts, source_label, detail_policy
        ))
    return await _read_review(review_id)


@router.get("/memory-review/latest")
async def get_latest_memory_review():
    await _ensure_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM seeky_memory_reviews WHERE mode='memory_compress' ORDER BY created_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return {"ok": True, "review": None}
    return {"ok": True, "review": await _read_review(row["id"])}


@router.get("/memory-review/{review_id}")
async def get_memory_review(review_id: str):
    return await _read_review(review_id)


@router.put("/memory-review/{review_id}")
async def update_memory_review(review_id: str, body: MemoryReviewUpdate):
    review = await _read_review(review_id)
    if review["status"] != "draft":
        raise HTTPException(status_code=400, detail="只有草案状态可以修改")

    existing = {item["seq"]: item for item in review["items"]}
    now = time.time()
    async with get_db() as db:
        for patch in body.items:
            seq = patch.seq.strip()
            item = existing.get(seq)
            if not item:
                continue
            action = _normalize_review_action(patch.final_action)
            final_content = (patch.final_content or "").strip()
            if review.get("mode") in {"source_day", "memory_compress"}:
                if action not in {"create", "discard"}:
                    action = "create"
                if action == "create" and not final_content:
                    final_content = item["original_content"]
                elif action == "discard":
                    final_content = ""
            elif action == "edit" and not final_content:
                action = "keep"
            elif action == "keep":
                final_content = item["original_content"]
            elif action == "delete":
                final_content = ""
            await db.execute(
                "UPDATE seeky_memory_review_items SET final_action=?, final_content=?, updated_at=? "
                "WHERE review_id=? AND seq=?",
                (action, final_content, now, review_id, seq),
            )
        await db.execute(
            "UPDATE seeky_memory_reviews SET updated_at=? WHERE id=?",
            (now, review_id),
        )
        await db.commit()
    return await _read_review(review_id)


@router.post("/memory-review/{review_id}/discard")
async def discard_memory_review(review_id: str):
    review = await _read_review(review_id)
    if review["status"] == "applied":
        raise HTTPException(status_code=400, detail="已应用的草案不能废弃")
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "UPDATE seeky_memory_reviews SET status='discarded', discarded_at=?, updated_at=? WHERE id=?",
            (now, now, review_id),
        )
        await db.commit()
    return await _read_review(review_id)


@router.post("/memory-review/{review_id}/apply")
async def apply_memory_review(review_id: str):
    """确认应用草案。只有这个接口会真正 UPDATE/DELETE memories。"""
    review = await _read_review(review_id)
    if review["status"] != "draft":
        raise HTTPException(status_code=400, detail="只有草案状态可以应用")

    if review.get("mode") == "memory_compress":
        start_ts = review.get("source_start_ts")
        end_ts = review.get("source_end_ts")
        if not start_ts or not end_ts:
            raise HTTPException(status_code=400, detail="草案缺少压缩时间窗")
        options = {}
        try:
            options = json.loads(review.get("review_options") or "{}")
        except Exception:
            options = {}
        source = options.get("compress_source") or "summary"

        changed = 0
        deleted = 0
        kept = 0
        skipped = []
        now = time.time()
        inserted = []
        old_rows = await _fetch_compress_candidates(start_ts, end_ts, source)

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            for old in old_rows:
                await db.execute("DELETE FROM memories WHERE id=?", (old["id"],))
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"seeky_apply_{time.time_ns()}", review_id, old["id"], "delete",
                        json.dumps(old, ensure_ascii=False), "", now,
                    ),
                )
                deleted += 1

            create_items = [item for item in review["items"] if _normalize_review_action(item["final_action"]) == "create"]
            for index, item in enumerate(create_items, start=1):
                content = (item["final_content"] or item["original_content"] or "").strip()
                if not content:
                    skipped.append({"seq": item["seq"], "reason": "内容为空"})
                    continue
                vec = await get_embedding(content)
                mem_id = f"mem_seeky_compress_{int((item.get('memory_time') or now) * 1000)}_{index}_{time.time_ns() % 100000}"
                keywords = item.get("original_keywords") or "[]"
                importance = item.get("original_importance")
                try:
                    importance = float(importance)
                except Exception:
                    importance = 0.5
                unresolved = 1 if item.get("original_unresolved") else 0
                memory_time = item.get("memory_time") or item.get("source_start_ts") or start_ts
                source_start = item.get("source_start_ts") or memory_time
                source_end = item.get("source_end_ts") or source_start
                source_msg_id = item.get("source_message_ids") or ""
                await db.execute(
                    "INSERT INTO memories ("
                    "id, content, type, created_at, source_conv, embedding, keywords, importance, "
                    "source_start_ts, source_end_ts, unresolved, source_msg_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mem_id, content, "seeky_compressed", memory_time, "seeky_memory_compress",
                        _pack_embedding(vec) if vec else None, keywords, importance,
                        source_start, source_end, unresolved, source_msg_id,
                    ),
                )
                after_dict = {
                    "id": mem_id,
                    "content": content,
                    "type": "seeky_compressed",
                    "created_at": memory_time,
                    "keywords": keywords,
                    "importance": importance,
                    "source_start_ts": source_start,
                    "source_end_ts": source_end,
                    "unresolved": unresolved,
                    "source_msg_id": source_msg_id,
                }
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"seeky_apply_{time.time_ns()}", review_id, mem_id, "create", "",
                        json.dumps(after_dict, ensure_ascii=False), now,
                    ),
                )
                inserted.append(after_dict)
                changed += 1

            kept = len(review["items"]) - len(create_items)
            await db.execute(
                "UPDATE seeky_memory_reviews SET status='applied', applied_at=?, updated_at=? WHERE id=?",
                (now, now, review_id),
            )
            await db.commit()

        for mem in inserted:
            await manager.broadcast({"type": "memory_added", "data": mem})

        result = await _read_review(review_id)
        result["apply_result"] = {
            "changed": changed,
            "deleted": deleted,
            "kept": kept,
            "skipped": skipped,
        }
        return result

    if review.get("mode") == "source_day":
        start_ts = review.get("source_start_ts")
        end_ts = review.get("source_end_ts")
        if not start_ts or not end_ts:
            raise HTTPException(status_code=400, detail="草案缺少原文时间窗")

        changed = 0
        deleted = 0
        kept = 0
        skipped = []
        now = time.time()
        inserted = []

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, type, created_at, source_conv, keywords, importance, "
                "source_start_ts, source_end_ts, unresolved, source_msg_id "
                "FROM memories "
                "WHERE source_start_ts IS NOT NULL AND source_start_ts >= ? AND source_start_ts < ?",
                (start_ts, end_ts),
            )
            old_rows = [dict(row) for row in await cur.fetchall()]
            for old in old_rows:
                await db.execute("DELETE FROM memories WHERE id=?", (old["id"],))
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"seeky_apply_{time.time_ns()}", review_id, old["id"], "delete",
                        json.dumps(old, ensure_ascii=False), "", now,
                    ),
                )
                deleted += 1

            create_items = [item for item in review["items"] if _normalize_review_action(item["final_action"]) == "create"]
            for index, item in enumerate(create_items, start=1):
                content = (item["final_content"] or item["original_content"] or "").strip()
                if not content:
                    skipped.append({"seq": item["seq"], "reason": "内容为空"})
                    continue
                vec = await get_embedding(content)
                mem_id = f"mem_seeky_{int((item.get('memory_time') or now) * 1000)}_{index}_{time.time_ns() % 100000}"
                keywords = item.get("original_keywords") or "[]"
                importance = item.get("original_importance")
                try:
                    importance = float(importance)
                except Exception:
                    importance = 0.5
                unresolved = 1 if item.get("original_unresolved") else 0
                memory_time = item.get("memory_time") or item.get("source_start_ts") or start_ts
                source_start = item.get("source_start_ts") or memory_time
                source_end = item.get("source_end_ts") or source_start
                source_msg_id = item.get("source_message_ids") or ""
                await db.execute(
                    "INSERT INTO memories ("
                    "id, content, type, created_at, source_conv, embedding, keywords, importance, "
                    "source_start_ts, source_end_ts, unresolved, source_msg_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mem_id, content, "seeky_digest", memory_time, None,
                        _pack_embedding(vec) if vec else None, keywords, importance,
                        source_start, source_end, unresolved, source_msg_id,
                    ),
                )
                after_dict = {
                    "id": mem_id,
                    "content": content,
                    "type": "seeky_digest",
                    "created_at": memory_time,
                    "keywords": keywords,
                    "importance": importance,
                    "source_start_ts": source_start,
                    "source_end_ts": source_end,
                    "unresolved": unresolved,
                    "source_msg_id": source_msg_id,
                }
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"seeky_apply_{time.time_ns()}", review_id, mem_id, "create", "",
                        json.dumps(after_dict, ensure_ascii=False), now,
                    ),
                )
                inserted.append(after_dict)
                changed += 1

            kept = len(review["items"]) - len(create_items)
            await db.execute(
                "UPDATE seeky_memory_reviews SET status='applied', applied_at=?, updated_at=? WHERE id=?",
                (now, now, review_id),
            )
            await db.commit()

        for mem in inserted:
            await manager.broadcast({"type": "memory_added", "data": mem})

        result = await _read_review(review_id)
        result["apply_result"] = {
            "changed": changed,
            "deleted": deleted,
            "kept": kept,
            "skipped": skipped,
        }
        return result

    changed = 0
    deleted = 0
    kept = 0
    skipped = []
    now = time.time()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for item in review["items"]:
            action = _normalize_review_action(item["final_action"])
            if action == "keep":
                kept += 1
                continue

            cur = await db.execute(
                "SELECT id, content, type, created_at, source_conv, keywords, importance, "
                "source_start_ts, source_end_ts, unresolved, source_msg_id "
                "FROM memories WHERE id=?",
                (item["mem_id"],),
            )
            current = await cur.fetchone()
            if not current:
                skipped.append({"seq": item["seq"], "mem_id": item["mem_id"], "reason": "记忆已不存在"})
                continue
            current_dict = dict(current)
            if current_dict["content"] != item["original_content"]:
                skipped.append({"seq": item["seq"], "mem_id": item["mem_id"], "reason": "原记忆已被其他地方改动，跳过"})
                continue

            before_json = json.dumps(current_dict, ensure_ascii=False)
            log_id = f"seeky_apply_{time.time_ns()}"

            if action == "delete":
                await db.execute("DELETE FROM memories WHERE id=?", (item["mem_id"],))
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (log_id, review_id, item["mem_id"], "delete", before_json, "", now),
                )
                deleted += 1
                continue

            final_content = (item["final_content"] or "").strip()
            if action == "edit" and final_content and final_content != current_dict["content"]:
                vec = await get_embedding(final_content)
                await db.execute(
                    "UPDATE memories SET content=?, embedding=? WHERE id=?",
                    (final_content, _pack_embedding(vec) if vec else None, item["mem_id"]),
                )
                after_dict = dict(current_dict)
                after_dict["content"] = final_content
                await db.execute(
                    "INSERT INTO seeky_memory_apply_log (id, review_id, mem_id, action, before_json, after_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        log_id, review_id, item["mem_id"], "edit", before_json,
                        json.dumps(after_dict, ensure_ascii=False), now,
                    ),
                )
                changed += 1
            else:
                kept += 1

        await db.execute(
            "UPDATE seeky_memory_reviews SET status='applied', applied_at=?, updated_at=? WHERE id=?",
            (now, now, review_id),
        )
        await db.commit()

    result = await _read_review(review_id)
    result["apply_result"] = {
        "changed": changed,
        "deleted": deleted,
        "kept": kept,
        "skipped": skipped,
    }
    return result
