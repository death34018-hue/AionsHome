"""
聊天室核心逻辑：Connor 代理调用、跨窗口上下文构建、AI 互聊控制、聊天室记忆管理
"""

import json, time, struct, asyncio, uuid, os, threading
from typing import Optional
from pathlib import Path

import aiosqlite, httpx

from config import DATA_DIR, DEFAULT_MODEL, MODELS, load_worldbook
from database import get_db
from model_json import extract_json_object
from memory import (
    get_embedding, cosine_similarity, _pack_embedding, _unpack_embedding, _keyword_match_score,
    _memory_time_payload, _format_raw_evidence_block,
)
from ai_providers import call_codex_cli, stream_ai, CLI_STATUS_PREFIX, _build_cli_prompt
from context_builder import build_ability_block, build_memory_blocks, fetch_merged_timeline, render_merged_timeline
from ws import manager

# ── Connor-Codex 服务配置 ──
CHATROOM_CONFIG_PATH = DATA_DIR / "chatroom_config.json"
_CHATROOM_CONFIG_LOCK = threading.Lock()

_DEFAULT_CONFIG = {
    "connor_url": "http://127.0.0.1:8787",
    "connor_poll_interval": 1.0,
    "connor_poll_timeout": 480,  # 8 分钟，与 Connor 后端 CODEX_TIMEOUT_MS 保持一致
    "connor_name": "第二AI",
    "connor_persona": "",
    "connor_persona_sections": {},
    "connor_persona_evolution_enabled": False,
    "tts_enabled": False,
    "tts_aion_voice": "",
    "tts_connor_voice": "",
    "reply_order": "random",
    "connor_model": "Codex",
    "aion_model": "",
    "ambient_voice_enabled": False,
    "ambient_voice_wake_word": "现在立刻唤醒",
    "ambient_voice_stop_word": "结束立刻唤醒",
    "ambient_voice_min_chars": 500,
    "ambient_voice_interval_seconds": 120,
    "ambient_voice_cooldown_seconds": 180,
}


def load_chatroom_config() -> dict:
    if CHATROOM_CONFIG_PATH.exists():
        try:
            return {**_DEFAULT_CONFIG, **json.loads(CHATROOM_CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_chatroom_config(data: dict):
    """Atomically replace config so a killed/slow client cannot leave partial JSON."""
    CHATROOM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CHATROOM_CONFIG_PATH.with_suffix(CHATROOM_CONFIG_PATH.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with _CHATROOM_CONFIG_LOCK:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, CHATROOM_CONFIG_PATH)


def get_chatroom_names() -> tuple[str, str, str]:
    wb = load_worldbook()
    cfg = load_chatroom_config()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"
    connor_name = cfg.get("connor_name") or "第二AI"
    return user_name, ai_name, connor_name


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return [text]


def _source_ids_for_chatroom_memory(mem: dict) -> list[str]:
    ids = []
    for raw in _json_list(mem.get("source_msg_id")):
        source_id = str(raw).strip()
        if not source_id:
            continue
        if ":" not in source_id:
            source_id = f"chatroom:{source_id}"
        ids.append(source_id)
    return ids


def display_name_for_sender(sender: str) -> str:
    user_name, ai_name, connor_name = get_chatroom_names()
    return {"user": user_name, "assistant": ai_name, "aion": ai_name, "connor": connor_name}.get(sender, sender)


# ══════════════════════════════════════════════════
#  Connor 代理调用
# ══════════════════════════════════════════════════

async def send_to_connor(text: str, images: list[dict] = None) -> Optional[str]:
    """发送消息给 Connor-Codex 服务并通过 SSE /api/stream 等待回复。
    只有 POST 失败或 health 检测失败才返回 None（代表服务不可用）。
    任务超时（8分钟）仍返回 None，调用方可据此提示"任务仍在处理"。
    """
    cfg = load_chatroom_config()
    base = cfg["connor_url"].rstrip("/")
    timeout = cfg.get("connor_poll_timeout", 480)

    # 1. 发送用户消息，拿到 task_id
    payload = {"text": text}
    if images:
        payload["images"] = images
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{base}/api/messages", json=payload)
            if resp.status_code != 200:
                return None
            sent = resp.json().get("message", {})
            task_id = sent.get("id")
    except Exception:
        return None

    # 2. 连接 SSE /api/stream，监听 message 事件等待 assistant 回复
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=timeout + 30)) as client:
            async with client.stream("GET", f"{base}/api/stream") as sse_resp:
                buffer = ""
                deadline = asyncio.get_event_loop().time() + timeout
                async for raw_bytes in sse_resp.aiter_bytes():
                    buffer += raw_bytes.decode("utf-8", errors="replace")
                    # 按 SSE 协议解析：事件以双换行分隔
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        event_type = ""
                        data_lines = []
                        for line in block.split("\n"):
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                data_lines.append(line[6:])
                        if not data_lines:
                            continue
                        try:
                            data = json.loads("".join(data_lines))
                        except (json.JSONDecodeError, ValueError):
                            continue

                        # 监听 message 事件：匹配 taskId 的 assistant 回复
                        if event_type == "message" and data.get("role") == "assistant":
                            if data.get("taskId") == task_id:
                                return data.get("text", "")

                    # 检查超时
                    if asyncio.get_event_loop().time() > deadline:
                        return _CONNOR_TIMEOUT_SENTINEL
    except Exception:
        pass

    # 3. SSE 连接断开后，回退到单次查询，可能任务已经完成了
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/api/messages")
            msgs = resp.json().get("messages", [])
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("taskId") == task_id:
                    if m.get("status") != "running":
                        return m.get("text", "")
    except Exception:
        pass

    return _CONNOR_TIMEOUT_SENTINEL


# 超时哨兵值：区分"服务不可用(None)"和"任务仍在处理(超时)"
_CONNOR_TIMEOUT_SENTINEL = "__CONNOR_STILL_PROCESSING__"


async def check_connor_online() -> bool:
    """检查 Connor-Codex 服务是否在线"""
    cfg = load_chatroom_config()
    base = cfg["connor_url"].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{base}/api/health")
            return resp.status_code == 200
    except Exception:
        return False


# ══════════════════════════════════════════════════
#  Connor Codex CLI 直接调用
# ══════════════════════════════════════════════════

_CONNOR_PERSONA_PATH = Path(__file__).parent.parent / "Connor-Codex" / "persona.md"
_CONNOR_PERSONA_SECTION_LABELS = [
    ("identity_core", "核心身份"),
    ("relationship_core", "关系锚点"),
    ("personality_core", "人格与判断"),
    ("communication_style", "表达与互动方式"),
    ("boundaries_and_forbidden", "边界与禁忌"),
    ("relationship_protocol", "协议边界"),
    ("tool_and_capability_rules", "能力与工具规则"),
    ("prompt_hygiene_rules", "提示边界"),
    ("evolution_notes", "用户信息"),
]


def _compile_connor_persona_sections(sections: dict) -> str:
    parts = []
    for key, label in _CONNOR_PERSONA_SECTION_LABELS:
        value = (sections.get(key) or "").strip()
        if value:
            parts.append(f"[{label}]\n{value}")
    return "\n\n".join(parts)


def _read_connor_persona() -> str:
    """读取 Connor 全局人设：优先 chatroom_config，其次 persona.md 文件；勾选补充设定时拼接"""
    cfg = load_chatroom_config()
    cfg_sections = cfg.get("connor_persona_sections") or {}
    if not isinstance(cfg_sections, dict):
        cfg_sections = {}
    cfg_persona = _compile_connor_persona_sections(cfg_sections).strip() if cfg_sections else ""
    if not cfg_persona:
        cfg_persona = cfg.get("connor_persona", "").strip()
    base = cfg_persona
    if not base and _CONNOR_PERSONA_PATH.exists():
        base = _CONNOR_PERSONA_PATH.read_text(encoding="utf-8").strip()
    if cfg.get("connor_persona_extra_enabled") and cfg.get("connor_persona_extra", "").strip():
        extra = cfg["connor_persona_extra"].strip()
        base = f"{base}\n\n[补充设定]\n{extra}" if base else extra
    return base


def _build_connor_messages(prompt: str) -> list[dict]:
    """将 Connor prompt 包装为 messages 列表，注入 persona 作为 system"""
    persona = _read_connor_persona()
    messages = []
    if persona:
        messages.append({"role": "system", "content": persona})
    messages.append({"role": "user", "content": prompt})
    return messages


async def stream_connor_cli(prompt: str = None, *, messages: list[dict] = None, meta: dict | None = None):
    """流式调用 Codex CLI 获取 Connor 回复，yield text chunks 和 CLI_STATUS_PREFIX 状态。
    可传入纯文本 prompt（旧方式）或完整 messages 列表（保留附件图片）。"""
    if messages is None:
        messages = _build_connor_messages(prompt)
    else:
        # 注入 persona 作为 system（如果 messages 中没有）
        if not any(m["role"] == "system" for m in messages):
            persona = _read_connor_persona()
            if persona:
                messages = [{"role": "system", "content": persona}] + messages
    codex_model = (MODELS.get("Codex") or {}).get("model", "")
    async for chunk in call_codex_cli(messages, codex_model, meta):
        yield chunk


async def simple_connor_cli_call(
    prompt: str,
    model_key: str | None = None,
    *,
    trace_label: str = "chatroom_connor_call",
) -> Optional[str]:
    """非流式调用 Connor 模型，根据 connor_model 配置选择 Codex CLI 或 stream_ai"""
    if model_key is None:
        cfg = load_chatroom_config()
        model_key = cfg.get("connor_model") or "Codex"
    model_key = (model_key or "Codex").strip() or "Codex"
    full_text = ""
    if model_key == "Codex":
        async for chunk in stream_connor_cli(prompt):
            if not chunk.startswith(CLI_STATUS_PREFIX):
                full_text += chunk
    else:
        from ai_providers import simple_ai_call
        messages = [{"role": "user", "content": prompt}]
        full_text = await simple_ai_call(messages, model_key, trace_label=trace_label)
    return full_text.strip() or None


# ══════════════════════════════════════════════════
#  跨窗口上下文构建
# ══════════════════════════════════════════════════

async def get_main_chat_recent(minutes: int = 30, limit: int = 40) -> list[dict]:
    """从主聊天获取近 N 分钟的消息"""
    cutoff = time.time() - minutes * 60
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE created_at > ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


async def get_connor_1v1_recent(minutes: int = 30, limit: int = 40) -> list[dict]:
    """从 Connor 1v1 聊天室获取近 N 分钟的消息"""
    cutoff = time.time() - minutes * 60
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # 找到 connor_1v1 类型的房间
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type = 'connor_1v1' ORDER BY created_at ASC LIMIT 1"
        )
        room = await cur.fetchone()
        if not room:
            return []
        cur = await db.execute(
            "SELECT sender, content, created_at FROM chatroom_messages "
            "WHERE room_id = ? AND created_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (room["id"], cutoff, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


def format_cross_context(messages: list[dict], label: str) -> str:
    """将跨窗口消息格式化为上下文文本"""
    if not messages:
        return ""
    lines = [f"[{label} - 近期对话摘要]"]
    for m in messages:
        ts = time.strftime("%H:%M", time.localtime(m.get("created_at", 0)))
        role = m.get("role") or m.get("sender", "unknown")
        name = display_name_for_sender(role)
        text = (m.get("content") or "")[:300]
        lines.append(f"  [{ts}] {name}: {text}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
#  聊天室记忆系统
# ══════════════════════════════════════════════════

async def recall_chatroom_memories(
    query_text: str,
    room_id: str = "",
    scope: str = "group",
    query_keywords: list[str] = None,
    top_k: int = 5,
    threshold: float = 0.45,
    min_results: int = 0,
) -> list[dict]:
    """从 Connor 记忆库召回相关摘要记忆（所有聊天窗口共享）。"""
    select_cols = (
        "id, room_id, scope, scope AS type, content, keywords, importance, "
        "embedding, source_start_ts, source_end_ts, created_at, unresolved, source_msg_id, evidence_summary"
    )
    query_emb = await get_embedding(query_text)
    if not query_emb:
        if min_results <= 0:
            return []
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT {select_cols} FROM chatroom_memories "
                "WHERE scope = ? AND COALESCE(archive_state,'active')='active' "
                "ORDER BY unresolved DESC, importance DESC, created_at DESC LIMIT ?",
                (scope, min(top_k, min_results)),
            )
            rows = await cur.fetchall()
        fallback = []
        for row in rows:
            mem = dict(row)
            mem["score"] = 0.0
            mem["vec_sim"] = 0.0
            mem["kw_score"] = 0.0
            mem.update(_memory_time_payload(mem))
            fallback.append(mem)
        return fallback

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {select_cols} FROM chatroom_memories WHERE embedding IS NOT NULL "
            "AND scope = ? AND COALESCE(archive_state,'active')='active'",
            (scope,),
        )
        rows = await cur.fetchall()

    scored = []
    for row in rows:
        mem = dict(row)
        mem_emb = _unpack_embedding(mem["embedding"])
        vec_sim = cosine_similarity(query_emb, mem_emb)
        kw_score = _keyword_match_score(query_keywords or [], mem.get("keywords", "")) if query_keywords else 0
        importance = mem.get("importance", 0.5)
        final = vec_sim * 0.6 + kw_score * 0.3 + importance * 0.1
        mem["score"] = round(final, 4)
        mem["vec_sim"] = round(vec_sim, 4)
        mem["kw_score"] = round(kw_score, 4)
        mem["importance"] = round(float(importance or 0.5), 2)
        mem.update(_memory_time_payload(mem))
        scored.append(mem)

    scored.sort(key=lambda x: x["score"], reverse=True)
    matched = [m for m in scored if m["score"] >= threshold][:top_k]
    if min_results > 0 and len(matched) < min_results:
        seen_ids = {m["id"] for m in matched}
        for mem in scored:
            if len(matched) >= min(top_k, min_results):
                break
            if mem["id"] in seen_ids:
                continue
            matched.append(mem)
            seen_ids.add(mem["id"])
    return matched[:top_k]


async def build_surfacing_chatroom_memories(
    topic: str = "",
    keywords: list[str] = None,
    max_total: int = 8,
) -> tuple[list[dict], set]:
    """
    构建 Connor 侧的 [背景记忆] 注入内容（对标 Aion 的 build_surfacing_memories）。
    策略：
      1. unresolved 优先（最多 2 条）
      2. 话题相关浮现（topic embedding 匹配，最多 3 条）
      3. 近期补充（最近 3 天，补满 max_total）
    返回 (memories_list, surfaced_ids)。
    """
    surfaced_ids = set()
    result = []

    # 1. unresolved 优先
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, scope AS type, created_at, keywords, importance, unresolved, "
            "source_start_ts, source_end_ts, evidence_summary "
            "FROM chatroom_memories WHERE scope = ? AND unresolved = 1 "
            "AND COALESCE(archive_state,'active')='active' "
            "ORDER BY created_at DESC LIMIT 2",
            ("connor",),
        )
        unresolved_rows = await cur.fetchall()
    for row in unresolved_rows:
        item = {
            "id": row["id"],
            "content": row["content"],
            "created_at": row["created_at"],
            "source_start_ts": row["source_start_ts"],
            "source_end_ts": row["source_end_ts"],
            "evidence_summary": row["evidence_summary"] or "",
            "unresolved": True,
        }
        item.update(_memory_time_payload(item))
        result.append(item)
        surfaced_ids.add(row["id"])

    # 2. 话题相关浮现
    if topic and topic.strip() and len(result) < max_total:
        topic_vec = await get_embedding(topic)
        if topic_vec:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, content, scope AS type, created_at, embedding, keywords, importance, "
                    "source_start_ts, source_end_ts, evidence_summary "
                    "FROM chatroom_memories WHERE embedding IS NOT NULL "
                    "AND scope = ? AND COALESCE(archive_state,'active')='active'",
                    ("connor",),
                )
                rows = await cur.fetchall()
            scored = []
            for row in rows:
                if row["id"] in surfaced_ids:
                    continue
                mem_vec = _unpack_embedding(row["embedding"])
                sim = cosine_similarity(topic_vec, mem_vec)
                if sim >= 0.50:
                    scored.append({
                        "id": row["id"],
                        "content": row["content"],
                        "created_at": row["created_at"],
                        "source_start_ts": row["source_start_ts"],
                        "source_end_ts": row["source_end_ts"],
                        "evidence_summary": row["evidence_summary"] or "",
                        "sim": sim,
                        "unresolved": False,
                    })
            scored.sort(key=lambda x: x["sim"], reverse=True)
            for item in scored[:3]:
                if len(result) >= max_total:
                    break
                item.update(_memory_time_payload(item))
                result.append(item)
                surfaced_ids.add(item["id"])

    # 3. 近期补充（最近 3 天）
    if len(result) < max_total:
        three_days_ago = time.time() - 3 * 86400
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, scope AS type, created_at, source_start_ts, source_end_ts, evidence_summary FROM chatroom_memories "
                "WHERE scope = ? AND COALESCE(archive_state,'active')='active' "
                "AND COALESCE(source_end_ts, source_start_ts, created_at) > ? "
                "ORDER BY COALESCE(source_end_ts, source_start_ts, created_at) DESC LIMIT ?",
                ("connor", three_days_ago, max_total)
            )
            recent_rows = await cur.fetchall()
        for row in recent_rows:
            if len(result) >= max_total:
                break
            if row["id"] in surfaced_ids:
                continue
            item = {
                "id": row["id"],
                "content": row["content"],
                "created_at": row["created_at"],
                "source_start_ts": row["source_start_ts"],
                "source_end_ts": row["source_end_ts"],
                "evidence_summary": row["evidence_summary"] or "",
                "unresolved": False,
            }
            item.update(_memory_time_payload(item))
            result.append(item)
            surfaced_ids.add(row["id"])

    return result, surfaced_ids


async def fetch_chatroom_source_details(memories: list[dict], keywords: list[str]) -> str:
    """
    优先按 source_msg_id 返回这条记忆真正挂载的来源原文。
    旧记忆没有精确 source id 时，再按 source 时间范围和关键词回退追溯原文。
    """
    if not memories:
        return ""

    evidence_blocks = []
    fallback_memories = []
    user_name, ai_name, connor_name = get_chatroom_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    for mem in memories:
        source_ids = _source_ids_for_chatroom_memory(mem)
        if source_ids:
            rows = []
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                for source_id in source_ids:
                    if ":" not in source_id:
                        continue
                    prefix, raw_id = source_id.split(":", 1)
                    if prefix != "chatroom":
                        continue
                    cur = await db.execute(
                        "SELECT sender, content, created_at FROM chatroom_messages WHERE id=? AND sender != 'system'",
                        (raw_id,),
                    )
                    row = await cur.fetchone()
                    if row:
                        rows.append({
                            "name": name_map.get(row["sender"], row["sender"]),
                            "content": row["content"],
                            "created_at": row["created_at"],
                        })
            if rows:
                evidence_blocks.append(_format_raw_evidence_block(mem, rows))
                continue
        fallback_memories.append(mem)
    if not fallback_memories or not keywords:
        return "\n\n".join(evidence_blocks)

    kw_lower = [k.lower() for k in keywords if k.strip()]
    if not kw_lower:
        return "\n\n".join(evidence_blocks)

    for mem in fallback_memories:
        start_ts = mem.get("source_start_ts")
        end_ts = mem.get("source_end_ts")
        if not start_ts or not end_ts:
            continue
        seen = set()
        matched_rows = []
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT sender, content, created_at FROM chatroom_messages "
                "WHERE sender != 'system' AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (start_ts, end_ts)
            )
            rows = await cur.fetchall()
        for row in rows:
            content_lower = row["content"].lower()
            if any(kw in content_lower for kw in kw_lower):
                key = (row["created_at"], row["content"][:80])
                if key not in seen:
                    seen.add(key)
                    matched_rows.append({
                        "name": name_map.get(row["sender"], row["sender"]),
                        "content": row["content"],
                        "created_at": row["created_at"],
                    })
        if matched_rows:
            matched_rows.sort(key=lambda r: r["created_at"])
            evidence_blocks.append(_format_raw_evidence_block(mem, matched_rows[:8]))

    return "\n\n".join(evidence_blocks)


async def recall_main_chat_memories(
    query_text: str,
    query_keywords: list[str] = None,
    top_k: int = 3,
) -> list[dict]:
    """从主聊天记忆表中召回相关记忆（只读引用）"""
    from memory import recall_memories
    matched, _ = await recall_memories(query_text, query_keywords, top_k=top_k)
    return matched


async def save_chatroom_memory(
    room_id: str,
    scope: str,
    content: str,
    keywords: str = "",
    importance: float = 0.5,
    source_start_ts: float = None,
    source_end_ts: float = None,
    unresolved: int = 0,
    source_msg_id: str = None,
    memory_kind: str = "long_term",
    compression_stage: int = 0,
    created_at: float = None,
    evidence_summary: str = "",
    evidence_detail_level: str = "summary",
) -> Optional[str]:
    """保存一条聊天室记忆"""
    emb = await get_embedding(content)
    emb_blob = _pack_embedding(emb) if emb else None
    mem_id = f"crm_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    now = float(created_at) if created_at else time.time()
    kind = "daily" if memory_kind == "daily" else "long_term"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_memories "
            "(id, room_id, scope, content, keywords, importance, embedding, source_start_ts, source_end_ts, "
            "created_at, unresolved, source_msg_id, memory_kind, compression_stage, evidence_summary, evidence_detail_level) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mem_id, room_id, scope, content, keywords, importance, emb_blob,
                source_start_ts, source_end_ts, now, unresolved, source_msg_id, kind, compression_stage,
                evidence_summary or "", evidence_detail_level or "summary",
            ),
        )
        await db.commit()
    return mem_id


async def digest_chatroom(room_id: str = None, model_key: str = None, allow_ai_wishes: bool = False) -> dict:
    """对 Connor 的所有消息（1v1 + 群聊）统一进行总结，通过 Codex 生成记忆。
    支持分组（每批 20-50 条），总结后生成日记 + 可选朋友圈 + 礼物判断。
    room_id 参数保留兼容性但不再用于限定数据源。"""

    # 读取统一锚点（以 "connor_unified" 为 key）
    anchor_key = "connor_unified"
    covered_ids = set()
    digest_window_last_ts = 0
    try:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT anchor_ts FROM chatroom_digest_anchors WHERE room_id = ?", (anchor_key,))
            row = await cur.fetchone()
            anchor_ts = row["anchor_ts"] if row else 0

            # ── Connor 1v1 消息 ──
            cur = await db.execute(
                "SELECT id FROM chatroom_rooms WHERE type = 'connor_1v1' ORDER BY updated_at DESC LIMIT 1"
            )
            connor_room = await cur.fetchone()
            msgs = []
            if connor_room:
                cur = await db.execute(
                    "SELECT id, sender, content, created_at FROM chatroom_messages "
                    "WHERE room_id = ? AND created_at > ? AND sender != 'system' "
                    "ORDER BY created_at ASC",
                    (connor_room["id"], anchor_ts),
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    d["_source"] = "private"
                    d["_source_id"] = f"chatroom:{d['id']}"
                    msgs.append(d)

            # ── 群聊消息 ──
            cur = await db.execute(
                "SELECT id FROM chatroom_rooms WHERE type = 'group' ORDER BY updated_at DESC LIMIT 1"
            )
            group_room = await cur.fetchone()
            if group_room:
                cur = await db.execute(
                    "SELECT id, sender, content, created_at FROM chatroom_messages "
                    "WHERE room_id = ? AND created_at > ? AND sender != 'system' "
                    "ORDER BY created_at ASC",
                    (group_room["id"], anchor_ts),
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    d["_source"] = "group"
                    d["_source_id"] = f"chatroom:{d['id']}"
                    msgs.append(d)

            # 按时间排序合并
            msgs.sort(key=lambda x: x["created_at"])
            if msgs:
                digest_window_last_ts = msgs[-1]["created_at"]
                from homecoming.summary_coverage import filter_uncovered
                msgs, covered_ids = await filter_uncovered(
                    db, "second", msgs
                )
    except Exception as e:
        print(f"[chatroom_digest] 读取待总结消息失败，锚点未变: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "message": f"读取待总结消息失败，锚点未变：{type(e).__name__}",
            "new_memories_count": 0,
            "processed_messages": 0,
        }

    if not msgs and covered_ids:
        try:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO chatroom_digest_anchors "
                    "(room_id, anchor_ts) VALUES (?, ?)",
                    (anchor_key, digest_window_last_ts),
                )
                await db.commit()
        except Exception:
            pass
        return {
            "ok": True,
            "message": "当前没有新增内容需要总结",
            "new_memories_count": 0,
            "processed_messages": 0,
        }

    if len(msgs) < 40:
        return {"ok": False, "message": f"消息不足（{len(msgs)}条），至少需要 40 条"}

    # 读取世界书人设
    wb = load_worldbook()
    user_name, ai_name, connor_name = get_chatroom_names()

    # 构建人设前缀（Connor 总结只带自己的人设 + 用户信息，不带 Aion 人设）
    persona_block = ""
    connor_persona = _read_connor_persona()
    if connor_persona:
        persona_block += f"[{connor_name}的人设]\n{connor_persona}\n\n"
    if wb.get("user_persona"):
        persona_block += f"[{user_name}的信息]\n{wb['user_persona']}\n\n"

    # ── 分组（每批 20-50 条，均匀分配且绝不超过 50 条）──
    from memory import _atomic_digest_prompt, _normalize_digest_memory_items, _split_into_groups
    groups = _split_into_groups(msgs)
    total_new = 0
    all_summaries = []
    digest_incomplete = False
    store_room_id = connor_room["id"] if connor_room else (group_room["id"] if group_room else "connor_unified")

    for group in groups:
        # 构建消息文本
        group_start = time.strftime("%Y年%m月%d日 %H:%M", time.localtime(group[0]["created_at"]))
        group_end = time.strftime("%Y年%m月%d日 %H:%M", time.localtime(group[-1]["created_at"]))
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        sources = set(m.get("_source", "private") for m in group)
        has_mixed = len(sources) > 1
        formatted = []
        for m in group:
            ts = time.strftime("%m-%d %H:%M", time.localtime(m["created_at"]))
            name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(m["sender"], m["sender"])
            tag = f"[{'群聊' if m.get('_source') == 'group' else '私聊'}]" if has_mixed else ""
            formatted.append(f"[{ts}][id={m.get('_source_id', '')}]{tag} {name}: {m['content'][:300]}")
        messages_text = date_header + "\n".join(formatted)

        prompt_text = _atomic_digest_prompt(
            actor_name=connor_name,
            user_name=user_name,
            persona_block=persona_block,
            messages_text=messages_text,
            ai_name=ai_name,
            companion_name=connor_name,
        )

        full_text = await simple_connor_cli_call(
            prompt_text,
            model_key,
            trace_label="chatroom_digest_summary",
        )
        if not full_text:
            print(f"[chatroom_digest] Connor 模型无响应，跳过该组")
            digest_incomplete = True
            break

        result = _parse_digest_result(full_text)
        if not result:
            print(f"[chatroom_digest] JSON 解析失败: {full_text[:200]}")
            digest_incomplete = True
            break

        memory_items = _normalize_digest_memory_items(result, group)
        group_created = 0
        group_failed = False
        for item in memory_items:
            source_json = (
                json.dumps(item["source_message_ids"], ensure_ascii=False)
                if item["source_message_ids"] else None
            )
            try:
                mem_id = await save_chatroom_memory(
                    room_id=store_room_id,
                    scope="connor",
                    content=item["content"],
                    keywords=",".join(item["keywords"]),
                    importance=item["importance"],
                    source_start_ts=item["source_start_ts"],
                    source_end_ts=item["source_end_ts"],
                    unresolved=item["unresolved"],
                    source_msg_id=source_json,
                    memory_kind="daily" if item["memory_type"] == "daily" else "long_term",
                    evidence_summary=item["evidence_summary"],
                    evidence_detail_level="summary",
                )
            except Exception as e:
                print(f"[chatroom_digest] 记忆写入失败，保留锚点等待重试: {type(e).__name__}: {e}")
                group_failed = True
                break
            if mem_id:
                total_new += 1
                group_created += 1
                all_summaries.append(item["content"])
                await asyncio.sleep(0.001)

        if group_failed:
            digest_incomplete = True
            break
        if group_created > 0:
            try:
                async with get_db() as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO chatroom_digest_anchors (room_id, anchor_ts) VALUES (?, ?)",
                        (anchor_key, group[-1]["created_at"]),
                    )
                    await db.commit()
            except Exception as e:
                print(f"[chatroom_digest] 锚点保存失败，保留待重试: {type(e).__name__}: {e}")
                digest_incomplete = True
                break
        else:
            print(f"[chatroom_digest] 组 {group_start} ~ {group_end} 无可写入原子记忆")
            digest_incomplete = True
            break

    if (
        not digest_incomplete
        and total_new > 0
        and digest_window_last_ts > msgs[-1]["created_at"]
    ):
        try:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO chatroom_digest_anchors "
                    "(room_id, anchor_ts) VALUES (?, ?)",
                    (anchor_key, digest_window_last_ts),
                )
                await db.commit()
        except Exception:
            pass

    if total_new == 0:
        return {"ok": True, "message": f"总结完成：处理了 {len(msgs)} 条消息，但没有产出新的有效记忆；锚点已保留，可稍后重试", "new_memories_count": 0, "processed_messages": len(msgs)}

    # ── 总结完成后：生成日记；可选发布朋友圈 ──
    target_room_id = connor_room["id"] if connor_room else (group_room["id"] if group_room else None)
    if all_summaries and not digest_incomplete:
        try:
            connor_persona = _read_connor_persona()
            summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            context_lines = []
            for m in msgs[-30:]:
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                source_label = "群聊" if m.get("_source") == "group" else "私聊"
                ts = time.strftime("%m-%d %H:%M", time.localtime(m["created_at"]))
                name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(m["sender"], m["sender"])
                context_lines.append(f"[{ts}][{source_label}] {name}: {content[:300]}")
            context_text = "\n".join(context_lines)
            time_str = time.strftime("%Y年%m月%d日 %A %H:%M:%S", time.localtime())
            diary_prompt = (
                f"{persona_block}"
                f"当前时间：{time_str}\n\n"
                f"你是{connor_name}。你刚刚整理了和{user_name}今天的聊天记忆，以下是你整理出的摘要：\n"
                f"{summaries_text}\n\n"
                f"【最近上下文】\n{context_text}\n\n"
                f"请从你自己的视角写一篇私密日记，不是写给{user_name}看的聊天消息。"
                f"日记可以记录你对这段记忆的感想，只写值得记录或有感触的事，不用每件事都提起，不要记流水账。语气必须符合你的人设。"
                f"你可以自行决定是否发布一次朋友圈，朋友圈不用每次都发，有想吐槽或者感慨，或者搞笑的事情，或者用朋友圈隔空向对方喊话。\n"
                f"moment.expect_reply 表示发布朋友圈后是否希望另一个角色主动评论：true=希望对方回复，false=只发布、不触发回复；请根据朋友圈内容和你当下是否想与对方互动自行决定。\n"
                f"你可以自行决定是否给{user_name}送一份图片小礼物。送礼应是更低概率的特殊事件，只在特殊日子，或聊天中确实有特别温馨、感动、有意义、值得纪念的内容时才送；不要为了完成任务而送礼，也不要用礼物重复日记或朋友圈已经表达的普通感想。\n"
                f"givegift 为 true 时，gift.image_prompt 填写英文生图提示词，gift.message 填写符合你人设、自然真挚的赠言；为 false 时两项留空。\n\n"
                f"严格只输出 JSON，不要输出 Markdown，不要解释：\n"
                f"{{\n"
                f"  \"diary\": {{\"title\": \"日记标题\", \"content\": \"日记正文\", \"mood\": \"此刻心情\"}},\n"
                f"  \"post_moment\": false,\n"
                f"  \"moment\": {{\"content\": \"朋友圈内容，post_moment 为 false 时留空\", \"expect_reply\": false}},\n"
                f"  \"givegift\": false,\n"
                f"  \"gift\": {{\"image_prompt\": \"英文生图提示词，givegift 为 false 时留空\", \"message\": \"赠言，givegift 为 false 时留空\"}}\n"
                f"}}"
            )
            diary_text = await simple_connor_cli_call(
                diary_prompt,
                model_key,
                trace_label="chatroom_digest_diary",
            )

            from diary import normalize_diary_payload, parse_diary_payload, publish_ai_moment, save_diary_entry
            diary_data = parse_diary_payload(diary_text)
            if diary_data:
                diary_entry, moment_entry = normalize_diary_payload(diary_data)
                await save_diary_entry(
                    author="connor",
                    title=diary_entry.get("title", ""),
                    content=diary_entry.get("content", ""),
                    mood=diary_entry.get("mood", ""),
                    source_type="chatroom_digest",
                    source_ref=target_room_id or anchor_key,
                    source_start_ts=msgs[0]["created_at"],
                    source_end_ts=msgs[-1]["created_at"],
                )
                if moment_entry and moment_entry.get("content"):
                    await publish_ai_moment(
                        author="connor",
                        content=moment_entry.get("content", ""),
                        expect_reply=bool(moment_entry.get("expect_reply")),
                        source_conv=f"chatroom:{target_room_id}" if target_room_id else "chatroom:connor_unified",
                        source_msg_id=None,
                    )
                try:
                    from gift import send_gift_from_decision
                    await send_gift_from_decision(diary_data, sender="connor")
                except Exception as e:
                    print(f"[chatroom_digest] 执行送礼决定失败: {e}")
        except Exception as e:
            print(f"[chatroom_digest] 生成日记失败: {e}")

    ai_wish_created = False
    if allow_ai_wishes and total_new > 0 and all_summaries and not digest_incomplete:
        try:
            wish_context_lines = []
            for m in msgs[-30:]:
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                source_label = "群聊" if m.get("_source") == "group" else "私聊"
                ts = time.strftime("%m-%d %H:%M", time.localtime(m["created_at"]))
                name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(m["sender"], m["sender"])
                wish_context_lines.append(f"[{ts}][{source_label}] {name}: {content[:300]}")
            context_text = "\n".join(wish_context_lines)

            async def _generate_wish_text(prompt: str):
                return await simple_connor_cli_call(
                    prompt,
                    model_key,
                    trace_label="chatroom_digest_wish",
                )

            from wish_pool import maybe_create_ai_digest_wish

            wish_result = await maybe_create_ai_digest_wish(
                actor="connor",
                actor_name=connor_name,
                user_name=user_name,
                summaries=all_summaries,
                context_text=context_text,
                persona_block=persona_block,
                source_ref=target_room_id or anchor_key,
                source_start_ts=msgs[0]["created_at"],
                source_end_ts=msgs[-1]["created_at"],
                generate_text=_generate_wish_text,
            )
            ai_wish_created = bool(wish_result.get("created"))
            if ai_wish_created:
                print(f"[chatroom_digest] AI wish created: {wish_result.get('wish', {}).get('id', '')}")
        except Exception as e:
            print(f"[chatroom_digest] wish decision failed: {e}")

    return {
        "ok": True,
        "message": (
            f"总结完成：处理了 {len(msgs)} 条消息（{len(groups)} 组），生成了 {total_new} 条新记忆"
            + ("；部分后续消息未完成，锚点停在最后成功写入的分组，可稍后继续重试" if digest_incomplete else "")
        ),
        "new_memories_count": total_new,
        "processed_messages": len(msgs),
        "ai_wish_created": ai_wish_created,
    }


def _parse_digest_result(raw: str) -> Optional[dict]:
    """解析 AI 总结结果的 JSON"""
    data = extract_json_object(raw)
    if data is not None:
        return data
    raw = (raw or "").strip()
    if "```" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
    # 尝试直接解析
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 尝试提取最外层 JSON 对象
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end])
        except Exception:
            pass
    # fallback: 整段作为 summary
    if len(raw) > 20:
        return {"summary": raw, "keywords": "", "importance": 0.5, "unresolved": False}
    return None


# ══════════════════════════════════════════════════
#  群聊上下文构建
# ══════════════════════════════════════════════════

async def build_aion_group_context(
    room_id: str,
    room_messages: list[dict],
    context_limit: int = 30,
    query_text: str = "",
    query_keywords: list[str] = None,
    *,
    digest_result: dict = None,
    whisper_mode: bool = False,
) -> list[dict]:
    """为 Aion 在群聊中构建完整上下文（含系统能力、记忆召回、时间感知）。
    room_messages 仅用于提取 recent_for_digest，实际消息历史由统一时间线构建。"""
    history = []
    context_limit = max(1, int(context_limit or 30))

    # 0. 注入世界书（和主聊天一致的人设）
    wb = load_worldbook()
    user_name, ai_name, connor_name = get_chatroom_names()
    if wb.get("ai_persona"):
        history.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        history.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        history.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        history.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    # 1. 注入系统能力
    ability_block = await build_ability_block(
        user_name,
        who="aion",
        whisper_mode=whisper_mode,
        include_private_whisper=True,
    )
    if ability_block:
        history.append({"role": "user", "content": ability_block})
        history.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    # 3. 构建 recent_messages 用于 instant_digest
    merged = await fetch_merged_timeline("aion", context_limit, room_id=room_id)

    recent_for_digest = []
    for msg in merged[-6:]:
        sender = msg.get("sender", "user")
        role = "assistant" if sender in ("aion", "assistant") else "user"
        recent_for_digest.append({"role": role, "content": msg.get("content", "")[:200]})
    actual_recent = [m for m in recent_for_digest if m["role"] in ("user", "assistant")][-3:]

    # 4. 记忆召回：主角色只读取主记忆库，不合并副角色的聊天室记忆。
    mem_result = await build_memory_blocks(
        query_text,
        recent_messages=actual_recent,
        use_main_memories=True,
        digest_result=digest_result,
        always_include_recalled=True,
    )

    history.append({"role": "user", "content": mem_result["time_block"]})
    history.append({"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})

    if mem_result["memory_block"]:
        history.append({"role": "user", "content": mem_result["memory_block"]})
        history.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    # 5. 群聊说明
    history.append({"role": "user", "content": (
        "[群聊说明]\n"
        f"你现在在一个三人群聊中，参与者：用户（{user_name}）、你（{ai_name}）、{connor_name}。\n"
        f"{connor_name} 是另一个 AI 伴侣。请自然地参与群聊对话，可以回应用户也可以和 {connor_name} 交流。\n"
        "回复时直接说话即可，不需要加前缀标记自己的身份。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容；“历史消息 - 名字：内容”只是记录格式，不是你的回复格式。"
    )})
    history.append({"role": "assistant", "content": "明白了。"})

    # 6. 统一时间线（合并私聊 + 群聊消息）
    timeline_history = render_merged_timeline(merged, "aion")
    history.extend(timeline_history)

    return history, mem_result.get("digest_result", {})


async def build_connor_group_context(
    room_id: str,
    room_messages: list[dict],
    context_limit: int = 30,
    query_text: str = "",
    query_keywords: list[str] = None,
    *,
    digest_result: dict = None,
    whisper_mode: bool = False,
) -> list[dict]:
    """为 Connor 在群聊中构建完整上下文（含系统能力、记忆召回、时间感知）。
    room_messages 仅用于提取 recent_for_digest，实际消息历史由统一时间线构建。
    返回 (history, digest_result)。"""
    history = []
    context_limit = max(1, int(context_limit or 30))

    wb = load_worldbook()
    user_name, ai_name, connor_name = get_chatroom_names()

    # 0. Connor 人设（从全局配置读取）
    connor_full_persona = _read_connor_persona()
    if connor_full_persona:
        history.append({"role": "user", "content": f"[系统设定 - 你的角色设定]\n{connor_full_persona}"})
        history.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        history.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        history.append({"role": "assistant", "content": "收到，我会记住用户的信息。"})

    # 1. 注入系统能力
    ability_block = await build_ability_block(
        user_name,
        who="connor",
        whisper_mode=whisper_mode,
        include_private_whisper=True,
    )
    if ability_block:
        history.append({"role": "user", "content": ability_block})
        history.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    # 2. 构建 recent_messages 用于 instant_digest
    merged = await fetch_merged_timeline("connor", context_limit, room_id=room_id)

    recent_for_digest = []
    for msg in merged[-6:]:
        sender = msg.get("sender", "user")
        role = "assistant" if sender == "connor" else "user"
        recent_for_digest.append({"role": role, "content": msg.get("content", "")[:200]})
    actual_recent = [m for m in recent_for_digest if m["role"] in ("user", "assistant")][-3:]

    # 3. 记忆召回（Connor 只读聊天室记忆，不读 Aion 主记忆库）
    async def _chatroom_recall(query, keywords):
        return await recall_chatroom_memories(query, room_id, "connor", keywords, top_k=5, min_results=3)

    async def _chatroom_surfacing(topic, keywords):
        return await build_surfacing_chatroom_memories(topic, keywords)

    async def _chatroom_source(memories, keywords):
        return await fetch_chatroom_source_details(memories, keywords)

    mem_result = await build_memory_blocks(
        query_text,
        recent_messages=actual_recent,
        use_main_memories=False,
        chatroom_recall_fn=_chatroom_recall,
        chatroom_surfacing_fn=_chatroom_surfacing,
        chatroom_source_fn=_chatroom_source,
        digest_result=digest_result,
        always_include_recalled=True,
    )

    history.append({"role": "user", "content": mem_result["time_block"]})
    history.append({"role": "assistant", "content": "收到。"})

    if mem_result["memory_block"]:
        history.append({"role": "user", "content": mem_result["memory_block"]})
        history.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    # 4. 群聊说明
    history.append({"role": "user", "content": (
        "[群聊说明]\n"
        f"你现在在一个三人群聊中，参与者：用户（{user_name}）、{ai_name}（另一个AI）、你（{connor_name}）。\n"
        f"请自然地参与群聊对话，可以回应用户也可以和 {ai_name} 交流。\n"
        "回复时直接说话即可，不需要加前缀标记。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容；“历史消息 - 名字：内容”只是记录格式，不是你的回复格式。"
    )})
    history.append({"role": "assistant", "content": "明白了。"})

    # 5. 统一时间线（合并 Connor 1v1 + 群聊消息）
    merged = await fetch_merged_timeline("connor", context_limit, room_id=room_id)
    timeline_history = render_merged_timeline(merged, "connor")
    history.extend(timeline_history)

    return history, mem_result.get("digest_result", {})


async def build_connor_1v1_context(
    room_id: str,
    room_messages: list[dict],
    context_limit: int = 30,
    query_text: str = "",
    query_keywords: list[str] = None,
    *,
    digest_result: dict = None,
    whisper_mode: bool = False,
) -> tuple[list[dict], dict]:
    """为 Connor 1v1 聊天构建 messages 列表（含前置哨兵、背景浮现、原文追溯、附件图片）。
    返回 (messages, digest_result)。"""
    messages = []
    context_limit = max(1, int(context_limit or 30))

    wb = load_worldbook()
    user_name, _, _ = get_chatroom_names()

    # 角色设定、用户信息、能力等作为前缀消息对
    connor_full_persona = _read_connor_persona()
    if connor_full_persona:
        messages.append({"role": "user", "content": f"[你的角色设定]\n{connor_full_persona}"})
        messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    if wb.get("user_persona"):
        messages.append({"role": "user", "content": f"[{user_name}信息]\n{wb['user_persona']}"})
        messages.append({"role": "assistant", "content": "收到，我会记住用户的信息。"})

    ability_block = await build_ability_block(user_name, who="connor", whisper_mode=whisper_mode)
    if ability_block:
        messages.append({"role": "user", "content": ability_block})
    merged = await fetch_merged_timeline("connor", context_limit)
    if ability_block:
        messages.append({"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

    # 构建 recent_messages 用于 instant_digest（前置哨兵）
    recent_for_digest = []
    for msg in merged[-6:]:
        sender = msg.get("sender", "user")
        role = "assistant" if sender == "connor" else "user"
        recent_for_digest.append({"role": role, "content": msg.get("content", "")[:200]})
    actual_recent = [m for m in recent_for_digest if m["role"] in ("user", "assistant")][-3:]

    # 记忆召回（走统一 build_memory_blocks，含前置哨兵 + 背景浮现 + 原文追溯）
    async def _chatroom_recall(query, keywords):
        return await recall_chatroom_memories(query, room_id, "connor", keywords, top_k=5, min_results=3)

    async def _chatroom_surfacing(topic, keywords):
        return await build_surfacing_chatroom_memories(topic, keywords)

    async def _chatroom_source(memories, keywords):
        return await fetch_chatroom_source_details(memories, keywords)

    mem_result = await build_memory_blocks(
        query_text,
        recent_messages=actual_recent,
        use_main_memories=False,
        chatroom_recall_fn=_chatroom_recall,
        chatroom_surfacing_fn=_chatroom_surfacing,
        chatroom_source_fn=_chatroom_source,
        digest_result=digest_result,
        always_include_recalled=True,
    )

    messages.append({"role": "user", "content": mem_result["time_block"]})
    messages.append({"role": "assistant", "content": "收到。"})

    if mem_result["memory_block"]:
        messages.append({"role": "user", "content": mem_result["memory_block"]})
        messages.append({"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    messages.append({"role": "user", "content": (
        "[私聊说明]\n"
        "你现在在和用户的私聊窗口中。\n"
        "以下对话记录按时间线排列，可能包含私聊和群聊的混合内容，让你了解完整上下文；“历史消息 - 名字：内容”只是记录格式，不是你的回复格式。"
    )})
    messages.append({"role": "assistant", "content": "明白了。"})

    # 统一时间线（合并 Connor 1v1 + 群聊消息，保留附件）
    timeline_history = render_merged_timeline(merged, "connor")
    messages.extend(timeline_history)

    return messages, mem_result.get("digest_result", {})


# ══════════════════════════════════════════════════
#  Connor 自动总结（30 分钟无新消息自动触发，涵盖私聊+群聊）
# ══════════════════════════════════════════════════

_connor_last_msg_ts: float = 0.0       # 最后一条 Connor 相关消息的时间
_connor_digest_armed: bool = False     # 是否有待总结的新消息


def connor_1v1_on_message():
    """Connor 相关聊天产生新消息时调用（私聊或群聊），重置 30 分钟冷却"""
    global _connor_last_msg_ts, _connor_digest_armed
    _connor_last_msg_ts = time.time()
    _connor_digest_armed = True


async def _connor_1v1_auto_digest_loop():
    """后台循环：每 5 分钟检查一次，若 Connor 相关聊天已 30 分钟无新消息则自动总结"""
    global _connor_digest_armed
    while True:
        await asyncio.sleep(5 * 60)
        try:
            if not _connor_digest_armed:
                continue
            if _connor_last_msg_ts == 0:
                continue
            elapsed = time.time() - _connor_last_msg_ts
            if elapsed < 30 * 60:
                continue
            print(f"[chatroom_auto_digest] Connor 相关聊天已 {elapsed/60:.0f} 分钟无新消息，开始自动总结")
            result = await digest_chatroom(allow_ai_wishes=True)
            print(f"[chatroom_auto_digest] {result.get('message', '')}")
            # 总结完成后解除 armed，避免没有新消息时重复总结
            _connor_digest_armed = False
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[chatroom_auto_digest] ❌ 异常: {e}")
