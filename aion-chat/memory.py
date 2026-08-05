"""
向量记忆库：embedding、recall、手动总结、即时哨兵（RAG 路由）
"""

import json, time, struct, math, asyncio, re
from datetime import datetime, timedelta

import aiosqlite, httpx

from config import get_key, get_sentinel_config, get_embedding_config, load_worldbook, save_chat_status, load_digest_anchor, save_digest_anchor, DEFAULT_MODEL
from database import get_db
from model_json import extract_json_object
from ws import manager

# ── 向量工具 ──────────────────────────────────────
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMS = 3072


def _connor_display_name() -> str:
    try:
        from chatroom import load_chatroom_config
        return load_chatroom_config().get("connor_name") or "第二AI"
    except Exception:
        return "第二AI"


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


def _source_ids_for_memory(mem: dict) -> list[str]:
    ids = []
    source_conv = mem.get("source_conv") or ""
    for raw in _json_list(mem.get("source_msg_id")):
        source_id = str(raw).strip()
        if not source_id:
            continue
        if ":" not in source_id:
            prefix = "chatroom" if str(source_conv).startswith("chatroom:") else "private"
            source_id = f"{prefix}:{source_id}"
        ids.append(source_id)
    return ids


SUMMARY_MEMORY_TYPES = {"digest", "seeky_digest", "seeky_compressed", "daily"}
LONG_TERM_MEMORY_TYPE = "important"
DAILY_COMPRESSION_KEEP_SOURCE_DAYS = 180
DAILY_COMPRESSION_FINAL_STAGE = 4
DAILY_COMPRESSION_TIERS = [
    {
        "key": "recent",
        "label": "15-90d 近期轻整理",
        "min_days": 15,
        "max_days": 90,
        "source_stages": {0},
        "output_stage": 1,
        "retain_source_detail": True,
        "max_important": 2,
    },
    {
        "key": "mid",
        "label": "90-180d 中期整理",
        "min_days": 90,
        "max_days": 180,
        "source_stages": {0, 1},
        "output_stage": 2,
        "retain_source_detail": True,
        "max_important": 2,
    },
    {
        "key": "long",
        "label": "180-365d 远期归档",
        "min_days": 180,
        "max_days": 365,
        "source_stages": {0, 1, 2},
        "output_stage": 3,
        "retain_source_detail": False,
        "max_important": 4,
    },
    {
        "key": "archive",
        "label": "365d+ 事实档案",
        "min_days": 365,
        "max_days": None,
        "source_stages": {0, 1, 2, 3},
        "output_stage": 4,
        "retain_source_detail": False,
        "max_important": 6,
    },
]


def memory_kind_for_type(memory_type: str) -> str:
    """Two-bucket memory class: summary-style records are daily; everything else is long-term."""
    return "daily" if str(memory_type or "").strip().lower() in SUMMARY_MEMORY_TYPES else "long_term"


def memory_kind_label(memory_type: str) -> str:
    return "日常" if memory_kind_for_type(memory_type) == "daily" else "长期重要"


def _clean_evidence_summary(value, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _coerce_ts(value) -> float | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _format_ts_label(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _memory_time_payload(mem: dict) -> dict:
    start = _coerce_ts(mem.get("source_start_ts"))
    end = _coerce_ts(mem.get("source_end_ts"))
    created = _coerce_ts(mem.get("created_at"))
    if start:
        if end and abs(end - start) >= 60:
            if datetime.fromtimestamp(start).date() == datetime.fromtimestamp(end).date():
                label = (
                    f"发生：{_format_ts_label(start)}-"
                    f"{datetime.fromtimestamp(end).strftime('%H:%M')}"
                )
            else:
                label = f"发生：{_format_ts_label(start)} 至 {_format_ts_label(end)}"
        else:
            label = f"发生：{_format_ts_label(start)}"
        return {"memory_time": start, "memory_time_end": end or start, "memory_time_label": label}
    if created:
        return {"memory_time": created, "memory_time_end": created, "memory_time_label": f"记录：{_format_ts_label(created)}"}
    return {"memory_time": None, "memory_time_end": None, "memory_time_label": ""}


def _memory_line_with_evidence(mem: dict, limit: int = 220) -> str:
    content = str(mem.get("content") or "").strip()[:limit]
    time_label = mem.get("memory_time_label") or _memory_time_payload(mem).get("memory_time_label")
    if time_label:
        content = _LEADING_DATE_RE.sub("", content, count=1).strip()
    return f"- 记忆（{time_label}）：{content}" if time_label else f"- 记忆：{content}"


def format_recalled_memories_for_prompt(memories: list[dict], limit: int = 220) -> str:
    return "\n".join(_memory_line_with_evidence(mem, limit) for mem in memories)


async def _fetch_source_rows_by_ids(source_ids: list[str], user_name: str, ai_name: str) -> list[dict]:
    rows = []
    try:
        from chatroom import get_chatroom_names
        chat_user_name, chat_ai_name, companion_name = get_chatroom_names()
    except Exception:
        chat_user_name, chat_ai_name, companion_name = user_name, ai_name, "第二AI"
    chat_name_map = {"user": chat_user_name, "aion": chat_ai_name, "connor": companion_name}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for source_id in source_ids:
            if ":" not in source_id:
                continue
            prefix, raw_id = source_id.split(":", 1)
            if prefix == "private":
                cur = await db.execute(
                    "SELECT id, role, content, created_at FROM messages WHERE id=?",
                    (raw_id,),
                )
                row = await cur.fetchone()
                if row:
                    rows.append({
                        "id": f"private:{row['id']}",
                        "name": user_name if row["role"] == "user" else ai_name,
                        "content": row["content"],
                        "created_at": row["created_at"],
                    })
            elif prefix == "chatroom":
                cur = await db.execute(
                    "SELECT id, sender, content, created_at FROM chatroom_messages WHERE id=? AND sender != 'system'",
                    (raw_id,),
                )
                row = await cur.fetchone()
                if row:
                    rows.append({
                        "id": f"chatroom:{row['id']}",
                        "name": chat_name_map.get(row["sender"], row["sender"]),
                        "content": row["content"],
                        "created_at": row["created_at"],
                    })
    order = {source_id: i for i, source_id in enumerate(source_ids)}
    rows.sort(key=lambda r: (order.get(r["id"], 9999), r["created_at"]))
    return rows


def _format_raw_evidence_block(mem: dict, rows: list[dict], limit: int = 700) -> str:
    head = _memory_line_with_evidence(mem)
    lines = [head, "  来源原文："]
    for row in rows:
        ts = datetime.fromtimestamp(float(row["created_at"])).strftime("%m-%d %H:%M")
        text = re.sub(r"\s+", " ", str(row.get("content") or "")).strip()
        lines.append(f"  - [{ts}] {row.get('name', '')}: {text[:limit]}")
    return "\n".join(lines)


def _pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f'{len(values)}f', *values)


def _unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def get_embedding(text: str) -> list[float] | None:
    ecfg = get_embedding_config()
    if not ecfg["api_key"]:
        return None
    if ecfg["use_openai"]:
        # OpenAI 兼容格式（硅基流动等）
        url = f"{ecfg['base_url']}/v1/embeddings"
        headers = {"Authorization": f"Bearer {ecfg['api_key']}", "Content-Type": "application/json"}
        body = {"model": ecfg["model"], "input": text}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code != 200:
                    print(f"[Embedding] OpenAI 兼容调用失败 {resp.status_code}: {resp.text[:300]}")
                    return None
                return resp.json()["data"][0]["embedding"]
        except Exception as e:
            print(f"[Embedding] 调用异常: {e}")
            return None
    else:
        # Gemini 原生格式
        model = ecfg["model"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={ecfg['api_key']}"
        body = {"content": {"parts": [{"text": text}]}}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                return resp.json()["embedding"]["values"]
        except Exception:
            return None


# ── 关键词匹配辅助 ──────────────────────
def _keyword_match_score(query_keywords: list[str], mem_keywords_json: str) -> float:
    """计算关键词命中率：命中关键词数 / 查询关键词数"""
    if not query_keywords:
        return 0.0
    try:
        mem_kws = json.loads(mem_keywords_json) if mem_keywords_json else []
    except (json.JSONDecodeError, TypeError):
        mem_kws = []
    if not mem_kws:
        return 0.0
    mem_kws_lower = [k.lower() for k in mem_kws]
    hits = sum(1 for qk in query_keywords if any(qk.lower() in mk or mk in qk.lower() for mk in mem_kws_lower))
    return hits / len(query_keywords)


# ── 记忆召回（向量 + 关键词 + 重要度 综合评分）────
async def recall_memories(query_text: str, query_keywords: list[str] = None,
                          top_k: int = 5, threshold: float = 0.45) -> tuple[list[dict], list[dict]]:
    """
    综合评分 = 向量相似度×0.6 + 关键词命中率×0.3 + 重要度×0.1
    threshold 为最终得分门槛。
    返回 (matched, debug_top6): matched 为达标结果, debug_top6 为得分最高的前6条（含未达标）
    """
    query_vec = await get_embedding(query_text)
    if not query_vec:
        return [], []
    if query_keywords is None:
        query_keywords = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, embedding, keywords, importance, "
            "source_start_ts, source_end_ts, source_msg_id, evidence_summary "
            "FROM memories WHERE embedding IS NOT NULL "
            "AND COALESCE(archive_state,'active')='active'"
        )
        rows = await cur.fetchall()
    all_scored = []
    for row in rows:
        mem_vec = _unpack_embedding(row["embedding"])
        vec_sim = cosine_similarity(query_vec, mem_vec)
        kw_score = _keyword_match_score(query_keywords, row["keywords"]) if query_keywords else 0.0
        importance = float(row["importance"] or 0.5)
        final_score = vec_sim * 0.6 + kw_score * 0.3 + importance * 0.1
        item = {
            "id": row["id"], "content": row["content"], "type": row["type"],
            "created_at": row["created_at"],
            "score": round(final_score, 4),
            "vec_sim": round(vec_sim, 4),
            "kw_score": round(kw_score, 4),
            "importance": round(importance, 2),
            "keywords": row["keywords"] or "",
            "source_start_ts": row["source_start_ts"],
            "source_end_ts": row["source_end_ts"],
            "source_conv": row["source_conv"],
            "source_msg_id": row["source_msg_id"],
            "evidence_summary": row["evidence_summary"] if "evidence_summary" in row.keys() else "",
        }
        item.update(_memory_time_payload(item))
        all_scored.append(item)
    all_scored.sort(key=lambda x: x["score"], reverse=True)
    debug_top6 = all_scored[:6]
    matched = [r for r in all_scored if r["score"] >= threshold][:top_k]
    return matched, debug_top6


# ── 记忆证据：优先精确原文，旧数据回退范围筛选 ─────────────
async def fetch_source_details(memories: list[dict], keywords: list[str]) -> str:
    """
    优先按 source_msg_id 返回这条记忆真正挂载的来源原文。
    旧记忆没有精确 source id 时，再按 source 时间范围和关键词回退追溯原文。
    """
    if not memories:
        return ""

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    evidence_blocks = []
    fallback_memories = []
    for mem in memories:
        source_ids = _source_ids_for_memory(mem)
        if source_ids:
            rows = await _fetch_source_rows_by_ids(source_ids, user_name, ai_name)
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
            print(f"[source_detail] 跳过无时间范围的记忆: {mem.get('id','?')}")
            continue
        seen = set()
        matched_rows = []
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            # 私聊消息
            cur = await db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (start_ts, end_ts)
            )
            rows = list(await cur.fetchall())
            # 群聊消息
            cur = await db.execute(
                "SELECT id FROM chatroom_rooms WHERE type = 'group' ORDER BY updated_at DESC LIMIT 1"
            )
            group_room = await cur.fetchone()
            if group_room:
                cur = await db.execute(
                    "SELECT sender, content, created_at FROM chatroom_messages "
                    "WHERE room_id = ? AND created_at >= ? AND created_at <= ? AND sender != 'system' "
                    "ORDER BY created_at ASC",
                    (group_room["id"], start_ts, end_ts),
                )
                for gr in await cur.fetchall():
                    rows.append({"role": "assistant" if gr["sender"] == "aion" else "user",
                                 "content": gr["content"], "created_at": gr["created_at"],
                                 "_sender": gr["sender"]})
        print(f"[source_detail] 记忆 {mem.get('id','?')[:12]} 范围 {start_ts}-{end_ts}: 取到 {len(rows)} 条消息")
        hit_count = 0
        for row in rows:
            content_lower = row["content"].lower()
            if any(kw in content_lower for kw in kw_lower):
                key = (row["created_at"], row["content"][:80])
                if key not in seen:
                    seen.add(key)
                    sender = row["_sender"] if isinstance(row, dict) and "_sender" in row else ""
                    if sender:
                        connor_name = _connor_display_name()
                        name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(sender, sender)
                    else:
                        name = user_name if row["role"] == "user" else ai_name
                    matched_rows.append({
                        "name": name,
                        "content": row["content"],
                        "created_at": row["created_at"],
                    })
                    hit_count += 1
        print(f"[source_detail] → 关键词 {kw_lower} 命中 {hit_count} 条")
        if matched_rows:
            matched_rows.sort(key=lambda r: r["created_at"])
            evidence_blocks.append(_format_raw_evidence_block(mem, matched_rows[:8]))

    print(f"[source_detail] 最终返回 {len(evidence_blocks)} 个来源原文块")
    return "\n\n".join(evidence_blocks)


# ── 背景记忆浮现：unresolved + 话题相关 + 近期补充 ───
async def build_surfacing_memories(topic: str = "", keywords: list[str] = None,
                                    max_total: int = 8) -> tuple[list[dict], set]:
    """
    构建 [背景记忆] 注入内容。
    策略：
      1. unresolved 优先（最多 2 条）
      2. 话题相关浮现（topic embedding 匹配，最多 3 条）
      3. 近期补充（最近 3 天，补满 max_total）
    返回 (memories_list, surfaced_ids) 供后续 RAG 去重。
    """
    surfaced_ids = set()
    result = []

    # 1. unresolved 优先
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, keywords, importance, unresolved, "
            "source_start_ts, source_end_ts, evidence_summary "
            "FROM memories WHERE unresolved = 1 "
            "AND COALESCE(archive_state,'active')='active' "
            "ORDER BY created_at DESC LIMIT 2"
        )
        unresolved_rows = await cur.fetchall()
    for row in unresolved_rows:
        item = {
            "id": row["id"], "content": row["content"], "created_at": row["created_at"],
            "source_start_ts": row["source_start_ts"], "source_end_ts": row["source_end_ts"],
            "unresolved": True, "evidence_summary": row["evidence_summary"] or "",
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
                    "SELECT id, content, type, created_at, embedding, keywords, importance, "
                    "source_start_ts, source_end_ts, evidence_summary "
                    "FROM memories WHERE embedding IS NOT NULL "
                    "AND COALESCE(archive_state,'active')='active'"
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
                "SELECT id, content, type, created_at, source_start_ts, source_end_ts, evidence_summary FROM memories "
                "WHERE COALESCE(archive_state,'active')='active' "
                "AND COALESCE(source_end_ts, source_start_ts, created_at) > ? "
                "ORDER BY COALESCE(source_end_ts, source_start_ts, created_at) DESC LIMIT ?",
                (three_days_ago, max_total)
            )
            recent_rows = await cur.fetchall()
        for row in recent_rows:
            if len(result) >= max_total:
                break
            if row["id"] in surfaced_ids:
                continue
            result.append({
                "id": row["id"],
                "content": row["content"],
                "created_at": row["created_at"],
                "source_start_ts": row["source_start_ts"],
                "source_end_ts": row["source_end_ts"],
                "evidence_summary": row["evidence_summary"] or "",
                "unresolved": False,
            })
            result[-1].update(_memory_time_payload(result[-1]))
            surfaced_ids.add(row["id"])

    return result, surfaced_ids


# ── 哨兵/前置模型统一调用 ────────────────────────
def _extract_gemini_final_text(data: dict) -> str:
    """Return visible Gemini output while skipping Gemma thinking parts."""
    parts = data["candidates"][0]["content"].get("parts", [])
    visible = [
        part.get("text", "")
        for part in parts
        if part.get("text") and not part.get("thought")
    ]
    if not visible:
        visible = [part.get("text", "") for part in parts if part.get("text")]
    return "\n".join(text.strip() for text in visible if text.strip()).strip()


async def _call_sentinel_text(scfg: dict, prompt: str, timeout: int = 60) -> str | None:
    """统一调用哨兵模型（纯文本），支持 Gemini 原生和 OpenAI 兼容格式"""
    if scfg["use_openai"]:
        url = f"{scfg['base_url']}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {scfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": scfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4096,
            "enable_thinking": False,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                print(f"[Sentinel] OpenAI 兼容调用失败 {resp.status_code}: {resp.text[:500]}")
                raise Exception(f"Sentinel API {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    else:
        model = scfg["model"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={scfg['api_key']}"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"contents": contents, "safetySettings": safety_settings})
            resp.raise_for_status()
            data = resp.json()
            return _extract_gemini_final_text(data)


async def _call_sentinel_vision(scfg: dict, prompt: str, img_b64: str, mime_type: str = "image/jpeg", timeout: int = 60) -> str | None:
    """统一调用哨兵模型（带图片），支持 Gemini 原生和 OpenAI 兼容格式"""
    if scfg["use_openai"]:
        url = f"{scfg['base_url']}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {scfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": scfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}}
            ]}],
            "temperature": 0.3,
            "max_tokens": 4096,
            "enable_thinking": False,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                print(f"[Sentinel] OpenAI 兼容 Vision 调用失败 {resp.status_code}: {resp.text[:500]}")
                raise Exception(f"Sentinel Vision API {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    else:
        model = scfg["model"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={scfg['api_key']}"
        contents = [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": img_b64}}
        ]}]
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"contents": contents, "safetySettings": safety_settings})
            resp.raise_for_status()
            data = resp.json()
            return _extract_gemini_final_text(data)


# ── 即时哨兵：每次用户发消息后触发（RAG 路由） ────
_MEMORY_REFERENCE_RE = re.compile(
    r"(昨天|前天|上次|之前|以前|刚才|那天|前几天|还记得|记不记得|"
    r"看过|听过|说过|聊过|讲过|做过|吃过|买过|去过)"
)
_DETAIL_REQUEST_RE = re.compile(
    r"(讲的啥|讲什么|说的啥|叫什么|叫啥|名字|细节|具体|哪|什么|怎么|为什么|大概|内容)"
)


def _latest_user_text(recent_messages: list[dict]) -> str:
    for msg in reversed(recent_messages or []):
        if msg.get("role") == "user":
            return str(msg.get("content") or "").strip()
    if recent_messages:
        return str(recent_messages[-1].get("content") or "").strip()
    return ""


def _fallback_digest_topic(text: str, keywords: list[str]) -> str:
    keyword_text = "、".join(str(k).strip() for k in (keywords or []) if str(k).strip())
    if keyword_text:
        return f"当前话题：{keyword_text}"
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\[\[image:[^\]]+\]\]", "", text).strip()
    if not text:
        return "当前对话的记忆线索"
    if _MEMORY_REFERENCE_RE.search(text):
        return "询问过往对话或经历中的具体内容"
    return "当前闲聊话题"


_GROUP_ROUTING_META_PHRASES = (
    "first_responder",
    "优先回复对象",
    "回复判断",
    "回复路由",
    "哨兵判断",
    "查询优化路由",
    "谁先回复",
    "由谁先回复",
    "下一条ai回复",
    "下一条 AI 回复",
    "发言顺序",
)


def _sanitize_group_recall_fields(
    topic: str,
    keywords: list[str],
    participant_names: dict[str, str],
    latest_user: str,
) -> tuple[str, list[str]]:
    """Keep group reply-routing metadata out of vector recall inputs."""
    blocked_names = {
        str(participant_names.get(key) or "").strip()
        for key in ("user", "aion", "connor")
    }
    blocked_names.update({"aion", "connor"})
    blocked_names.discard("")

    def _contains_blocked_name(text: str) -> bool:
        lowered = text.casefold()
        return any(name.casefold() in lowered for name in blocked_names)

    cleaned_keywords = []
    for keyword in keywords or []:
        value = str(keyword or "").strip()
        lowered = value.casefold()
        if not value or _contains_blocked_name(value):
            continue
        if value == "哨兵" or any(phrase.casefold() in lowered for phrase in _GROUP_ROUTING_META_PHRASES):
            continue
        cleaned_keywords.append(value)

    topic_text = str(topic or "").strip()
    topic_lowered = topic_text.casefold()
    has_routing_meta = any(
        phrase.casefold() in topic_lowered
        for phrase in _GROUP_ROUTING_META_PHRASES
    )
    if has_routing_meta:
        topic_text = ""
    else:
        for name in sorted(blocked_names, key=len, reverse=True):
            topic_text = re.sub(re.escape(name), "", topic_text, flags=re.IGNORECASE)
        topic_text = re.sub(r"[\s、,，;；:：/|]+", " ", topic_text).strip(" -—_")

    if not topic_text:
        topic_text = _fallback_digest_topic(latest_user, cleaned_keywords)
    return topic_text, cleaned_keywords


async def instant_digest(
    recent_messages: list[dict],
    group_participants: dict[str, str] | None = None,
) -> dict:
    """
    用户每次发消息后即时调用 flash-lite，返回结构化 JSON：
    {is_search_needed, keywords, require_detail, status, topic, first_responder}

    group_participants 仅在群聊传入，键为 user/aion/connor，值为动态配置的显示名。
    此时 first_responder 返回 aion/connor/random，用于决定本轮谁先回复。
    """
    gemini_key = get_key("gemini_free")
    scfg = get_sentinel_config()
    if not scfg["api_key"] or not recent_messages:
        return {
            "is_search_needed": False, "keywords": [], "require_detail": False,
            "status": "", "topic": "", "first_responder": "random",
        }

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    participant_names = group_participants or {}

    def _speaker_label(message: dict) -> str:
        sender = str(message.get("sender") or "").strip().lower()
        if sender and sender in participant_names:
            return participant_names[sender]
        return user_name if message.get("role") == "user" else ai_name

    messages_text = "\n".join(
        f"{_speaker_label(m)}: {str(m.get('content') or '')[:200]}"
        for m in recent_messages
    )

    responder_instruction = ""
    if group_participants:
        group_ai_name = participant_names.get("aion") or ai_name
        group_connor_name = participant_names.get("connor") or "另一位AI"
        responder_instruction = (
            f'7. "first_responder": 只根据最新一条用户消息判断下一条应由谁先回复。'
            f'明确在问或点名“{group_ai_name}”就填"aion"，'
            f'明确在问或点名“{group_connor_name}”就填"connor"；'
            f'同时问两人、没有明确对象或拿不准就填"random"。注意否定语义。\n'
        )

    prompt = (
        f"你是一个 RAG 系统的查询优化路由。分析用户输入，输出 JSON：\n"
        f"1. 忽略高频对话称呼：不要提取对话者的名字或昵称（如 \"{ai_name}\", \"{user_name}\", \"亲爱的\", \"老公\", \"宝贝\"）作为关键词。\n"
        f"2. 忽略高频常用词：如\"晚安故事\",\"吃什么\"等。\n"
        f"3. 聚焦核心实体：只提取稀缺的、具有区分度的名词（地点、物品、特定事件、专有名词等）\n"
        f"4. 判断召回记忆后是否需要附带对应的历史原文。记忆摘要每轮都会搜索；只要用户在问过去发生/看过/聊过/吃过/做过/提过的内容，或需要你回忆上下文事实，is_search_needed 必须为 true。\n"
        f"   \"is_search_needed\": Boolean.\n"
        f"      - false: 只召回并携带相关记忆摘要，不附带其历史原文。\n"
        f"      - true: 除相关记忆摘要外，还附带对应的历史原文；出现“昨天/前天/上次/之前/刚才/那天/看过/聊过/吃过/叫什么/讲的啥/还记得”等过去线索或事实追问时必须为 true。\n"
        f"   \"keywords\": 提取 2-5 个搜索关键词（过滤掉 {ai_name}, {user_name} 等高频人名）。如果没有专名，也要提取当前问题里的对象词、事件类型词或行为词，不要编造对话里没出现的词。\n"
        f"   \"require_detail\": Boolean.\n"
        f"      - false: 模糊回忆/情感抒发（只需读取摘要）。\n"
        f"      - true: 当询问具体事实/细节/名字/剧情/步骤/时间线（需要读取正文）时为 true，例如“前天看的电影讲的啥”“那个叫什么”“具体怎么说的”。\n"
        f"5. \"status\": 结合上下文总结{user_name}当前所处的状态（如：{user_name}刚吃完晚饭准备出门、洗完澡准备睡觉、回到家开始工作了等）。\n"
        f"6. \"topic\": 必须输出一个 8-40 字的检索话题摘要，永远不要留空。不要复制整句原话；要概括成短查询，例如“询问某次看过内容的剧情”“回忆之前提到的食物”等。\n\n"
        f"{responder_instruction}"
        f"严格只输出一个 JSON 对象，不要输出任何其他内容。\n\n"
        f"对话：\n{messages_text}"
    )

    try:
        raw = await _call_sentinel_text(scfg, prompt, timeout=15)
        if not raw:
            return {
                "is_search_needed": False, "keywords": [], "require_detail": False,
                "status": "", "topic": "", "first_responder": "random",
            }

        # 提取 JSON（可能包裹在 ```json ... ``` 中）
        if "```" in raw:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

        result = json.loads(raw)
        is_search = bool(result.get("is_search_needed", False))
        keywords = result.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("、", ",").split(",") if k.strip()]
        require_detail = bool(result.get("require_detail", False))
        status = str(result.get("status", "")).strip()
        first_responder = str(result.get("first_responder", "random")).strip().lower()
        if first_responder not in ("aion", "connor"):
            first_responder = "random"

        if status:
            save_chat_status(status)
            await manager.broadcast({"type": "chat_status", "data": {"status": status, "updated_at": time.time()}})

        topic = str(result.get("topic", "")).strip()
        latest_user = _latest_user_text(recent_messages)
        if group_participants:
            topic, keywords = _sanitize_group_recall_fields(
                topic,
                keywords,
                participant_names,
                latest_user,
            )
        if latest_user:
            if _MEMORY_REFERENCE_RE.search(latest_user):
                is_search = True
            if _DETAIL_REQUEST_RE.search(latest_user):
                require_detail = True
        if not topic:
            topic = _fallback_digest_topic(latest_user, keywords)

        return {
            "is_search_needed": is_search,
            "keywords": keywords,
            "require_detail": require_detail,
            "status": status,
            "topic": topic,
            "first_responder": first_responder,
        }
    except Exception:
        return {
            "is_search_needed": False, "keywords": [], "require_detail": False,
            "status": "", "topic": "", "first_responder": "random",
        }


# ── 手动总结：分组提取记忆 ─────────────────────────

def _split_into_groups(msgs: list, group_size: int = 50, min_group_size: int = 20) -> list[list]:
    """按 20-50 条均匀分组，保证任何一批都不突破 group_size 上限。"""
    total = len(msgs)
    if total <= group_size:
        return [msgs]

    group_count = math.ceil(total / group_size)
    base_size, larger_groups = divmod(total, group_count)
    if base_size < min_group_size:
        raise ValueError("消息数量无法同时满足分组上下限")
    groups = []
    offset = 0
    for index in range(group_count):
        size = base_size + (1 if index < larger_groups else 0)
        groups.append(msgs[offset:offset + size])
        offset += size
    return groups


async def _call_flash_lite(prompt: str) -> dict | None:
    """调用哨兵模型，返回 JSON 结果（仅供即时哨兵使用）"""
    scfg = get_sentinel_config()
    if not scfg["api_key"]:
        return None
    try:
        raw = await _call_sentinel_text(scfg, prompt, timeout=60)
        if not raw:
            return None
        # 提取 JSON
        if "```" in raw:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
        return json.loads(raw)
    except Exception:
        return None


def _parse_json_response(raw: str) -> dict | None:
    """从模型输出中提取 JSON 对象"""
    return extract_json_object(raw)


def _normalize_digest_keywords(value, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("、", ",").replace("，", ",").split(",")
    else:
        raw_items = _json_list(value)
    result = []
    seen = set()
    for raw in raw_items:
        text = str(raw).strip()
        if len(text) < 2 or text in seen:
            continue
        result.append(text)
        seen.add(text)
        if len(result) >= limit:
            break
    return result


def _source_map_for_digest_group(group: list[dict]) -> dict[str, dict]:
    source_by_id = {}
    for m in group:
        source_id = str(m.get("_source_id") or "").strip()
        if not source_id:
            continue
        source_by_id[source_id] = m
        raw_id = source_id.split(":", 1)[-1].strip()
        if raw_id and raw_id not in source_by_id:
            source_by_id[raw_id] = m
    return source_by_id


def _valid_digest_source_ids(value, source_by_id: dict[str, dict], limit: int = 6) -> list[str]:
    ids = []
    seen = set()
    for raw in _json_list(value):
        source_id = str(raw).strip()
        source_row = source_by_id.get(source_id)
        canonical_id = str((source_row or {}).get("_source_id") or source_id).strip()
        if source_row and canonical_id and canonical_id not in seen:
            ids.append(canonical_id)
            seen.add(canonical_id)
        if len(ids) >= limit:
            break
    return ids


_LEADING_DATE_RE = re.compile(
    r"^\s*(?:"
    r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}(?:日|号)?"
    r"|\d{1,2}月\d{1,2}(?:日|号)"
    r")(?:\s*(?:上午|下午|晚上|凌晨|中午|早上|傍晚|深夜)?\s*\d{1,2}[:：]\d{2})?"
    r"[，,。:：、\s]*"
)
_STRICT_DATE_PREFIX_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")


def _date_prefix_for_ts(ts: float | int | str | None) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _prefix_memory_content_date(content: str, ts: float | int | str | None) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return ""
    date_prefix = _date_prefix_for_ts(ts)
    if _STRICT_DATE_PREFIX_RE.match(text):
        return text
    text = _LEADING_DATE_RE.sub("", text, count=1).strip()
    return f"{date_prefix}，{text}"


def _replace_relative_time_terms(content: str, ts: float | int | str | None) -> str:
    """Make common relative time words safe once the memory has a source date."""
    text = str(content or "")
    try:
        base_dt = datetime.fromtimestamp(float(ts))
    except Exception:
        base_dt = datetime.now()

    def day(offset: int) -> str:
        return (base_dt + timedelta(days=offset)).strftime("%Y-%m-%d")

    prev_month = base_dt.replace(day=1) - timedelta(days=1)
    prev_week_start = base_dt - timedelta(days=base_dt.weekday() + 7)
    prev_week_end = prev_week_start + timedelta(days=6)
    recent_window = f"{day(0)}前后这段时间"
    replacements = {
        "大前天": day(-3),
        "前天": day(-2),
        "昨天": day(-1),
        "今天": day(0),
        "当天": day(0),
        "明天": day(1),
        "最近": recent_window,
        "近期": recent_window,
        "这几天": recent_window,
        "前几天": f"{day(0)}前几日",
        "上周": f"{prev_week_start.strftime('%Y-%m-%d')}至{prev_week_end.strftime('%Y-%m-%d')}",
        "上个月": prev_month.strftime("%Y-%m"),
        "刚才": "此前不久",
        "当时": "当时",
        "那天": day(0),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


_DIGEST_ITEM_RE = re.compile(
    r'\{\s*"content"\s*:\s*"(?P<content>[\s\S]*?)"\s*,\s*'
    r'"(?:type|memory_type)"\s*:\s*"(?P<type>[^"]*)"\s*,\s*'
    r'"keywords"\s*:\s*\[(?P<keywords>[\s\S]*?)\]\s*,\s*'
    r'"importance"\s*:\s*(?P<importance>-?\d+(?:\.\d+)?)\s*,\s*'
    r'"unresolved"\s*:\s*(?P<unresolved>true|false|0|1)\s*,\s*'
    r'"source_message_ids"\s*:\s*\[(?P<source_ids>[\s\S]*?)\]\s*'
    r'\}',
    re.IGNORECASE,
)


def _recover_digest_memory_items_from_text(text: str) -> list[dict]:
    """Recover memory items from model output that is JSON-shaped but invalid."""
    recovered = []
    raw = str(text or "")
    for match in _DIGEST_ITEM_RE.finditer(raw):
        keywords = re.findall(r'"([^"]+)"', match.group("keywords") or "")
        source_ids = re.findall(r'"([^"]+)"', match.group("source_ids") or "")
        content = match.group("content") or ""
        content = content.replace('\\"', '"').replace("\\n", "\n").strip()
        try:
            importance = float(match.group("importance"))
        except Exception:
            importance = 0.5
        unresolved_raw = (match.group("unresolved") or "").lower()
        recovered.append({
            "content": content,
            "type": match.group("type") or "daily",
            "keywords": keywords,
            "importance": importance,
            "unresolved": unresolved_raw in {"true", "1"},
            "source_message_ids": source_ids,
        })
    return recovered


def _normalize_digest_memory_items(result: dict, group: list[dict]) -> list[dict]:
    """
    Normalize atomic digest output. Legacy single-summary output is accepted as a fallback.
    """
    if not isinstance(result, dict):
        return []
    for nested_key in ("content", "text", "output", "response"):
        nested = _parse_json_response(str(result.get(nested_key) or ""))
        if isinstance(nested, dict) and isinstance(nested.get("memories"), list):
            result = nested
            break
    source_by_id = _source_map_for_digest_group(group)
    raw_items = result.get("memories")
    if not isinstance(raw_items, list):
        raw_items = []
        legacy_summary = str(result.get("summary") or "").strip()
        nested_summary = _parse_json_response(legacy_summary)
        if isinstance(nested_summary, dict) and isinstance(nested_summary.get("memories"), list):
            raw_items = nested_summary["memories"]
        elif recovered_summary := _recover_digest_memory_items_from_text(legacy_summary):
            raw_items = recovered_summary
        elif legacy_summary:
            raw_items.append({
                "content": legacy_summary,
                "type": "daily",
                "keywords": result.get("keywords", []),
                "importance": result.get("importance", 0.5),
                "unresolved": result.get("unresolved", False),
                "source_message_ids": [],
            })
        legacy_important = result.get("important_memory")
        if isinstance(legacy_important, dict):
            raw_items.append({**legacy_important, "type": "important"})
    else:
        expanded_items = []
        for item in raw_items:
            if isinstance(item, dict):
                item_content = str(item.get("content") or "")
                nested = _parse_json_response(item_content)
                if isinstance(nested, dict) and isinstance(nested.get("memories"), list):
                    expanded_items.extend(nested["memories"])
                    continue
                if recovered_nested := _recover_digest_memory_items_from_text(item_content):
                    expanded_items.extend(recovered_nested)
                    continue
            expanded_items.append(item)
        raw_items = expanded_items

    normalized = []
    seen_content = set()
    group_start = group[0]["created_at"]
    group_end = group[-1]["created_at"]
    for item in raw_items[:16]:
        if not isinstance(item, dict):
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if len(content) < 4:
            continue
        content_key = content[:120]
        if content_key in seen_content:
            continue
        seen_content.add(content_key)

        raw_type = str(item.get("type") or item.get("memory_type") or "daily").strip().lower()
        memory_type = LONG_TERM_MEMORY_TYPE if raw_type in {"important", "long_term", "长期重要"} else "daily"
        try:
            raw_importance = float(item.get("importance", 0.5 if memory_type == "daily" else 0.0))
        except Exception:
            raw_importance = 0.5 if memory_type == "daily" else 0.0
        if memory_type == LONG_TERM_MEMORY_TYPE:
            if raw_importance < 0.75:
                continue
            importance = max(0.75, min(1.0, raw_importance))
        else:
            importance = max(0.1, min(0.7, raw_importance))

        source_ids = _valid_digest_source_ids(item.get("source_message_ids"), source_by_id, limit=8)
        if source_ids:
            source_rows = [source_by_id[source_id] for source_id in source_ids]
            source_start = min((m["created_at"] for m in source_rows), default=group_start)
            source_end = max((m["created_at"] for m in source_rows), default=group_end)
        else:
            source_start = group_start
            source_end = group_end
        date_keyword = _date_prefix_for_ts(source_start)
        content = _prefix_memory_content_date(content, source_start)
        content = _replace_relative_time_terms(content, source_start)
        keywords = _normalize_digest_keywords(item.get("keywords"), limit=8)
        if date_keyword not in keywords:
            keywords = [date_keyword] + keywords

        normalized.append({
            "content": content,
            "memory_type": memory_type,
            "keywords": keywords[:8],
            "importance": importance,
            "unresolved": 0,
            "evidence_summary": "",
            "source_message_ids": source_ids,
            "source_start_ts": source_start,
            "source_end_ts": source_end,
        })
    return normalized


def _atomic_digest_prompt(
    *,
    actor_name: str,
    user_name: str,
    persona_block: str,
    messages_text: str,
    ai_name: str = "",
    companion_name: str = "",
) -> str:
    ignored_names = [name for name in {actor_name, user_name, ai_name, companion_name} if name]
    ignored_text = "、".join(ignored_names) if ignored_names else "高频对话称呼"
    return (
        f"{persona_block}"
        f"你是{actor_name}，请以自己的视角整理和{user_name}相关的对话记忆。从对话里抽取多条【原子记忆】。\n\n"
        "原子记忆规则：\n"
        "1. content 的第一个字符必须是绝对日期，格式固定为“YYYY-MM-DD，……”。日期来自 source_message_ids 对应原文的发生时间，用公历数字写入正文，作为 embedding 的一部分。\n"
        "2. content 开头必须使用绝对日期。尽量不要在正文里使用“今天、昨天、前天、最近、近期、这几天、前几天、那天、当天、当时、刚才、上周、上个月”等相对时间；如果原文用了相对时间，优先按原文时间换算成绝对日期，换算不清也不要因此丢弃有价值的记忆。\n"
        "3. 一条记忆只记录同一天、同一个可独立召回的事：事实、偏好、计划、关系变化、项目状态、健康/安全信息、阶段性目标，或一次具体生活/互动场景。\n"
        "4. 如果一段对话同时讲了事业、饮食、睡前习惯、关系设定、功能测试、电影评价、某个好玩的梗或小插曲，尽量按日期和事情拆成多条；但不要因为拆分不完美而放弃输出有来源的记忆。\n"
        "5. 保留有信息增量的内容：明确的测试反馈、用户的判断标准、项目推进结论、可复述的有趣场景、重要情绪原因、关系氛围变化、会影响以后陪伴的生活线索。\n"
        "6. 丢掉普通流水账：常规吃喝睡、一次性操作状态、无结论的过程、泛泛的“做了很多事”、无聊的抱怨等等。除非它和健康/金钱/项目/长期习惯/特别有记忆点的场景直接相关。\n"
        "7. content 写成自然记忆，尽量具体，不要只写“用户讨论了某事”；要写出对象、动作、结论或场景。\n"
        "8. 不要输出解释型来源说明，不要写“这说明了什么”。来源原文由后端按 source_message_ids 读取真实消息。\n"
        "9. source_message_ids 能引用真实支撑消息时就填 1-6 个；找不到或拿不准时可以留空数组，不要为了凑来源而编造 id。\n"
        "10. 每 50 条消息通常产出 1-3 条 daily。宁可少写，也不要把普通流水账塞进记忆库。\n\n"
        "11. unresolved 必须固定输出 false。不要自行标记未完成。\n\n"
        "type 规则：\n"
        "- daily：有明确日期和对象的普通事件、短期目标、项目进展、具体测试反馈、有趣小事、关系氛围、可帮助自然陪伴的生活线索。普通流水账不要写。\n"
        "- important：非常重要，值得长期记录的记忆。这条当严格使用，严禁滥用。只能记录发生过的事实，例如宠物死了，分手，以及恋人明确要求你记住的事情。否则不能随意输出。\n\n"
        f"keywords：提取 1-5 个稀缺关键词，必须包含这条记忆的 YYYY-MM-DD 日期关键词；过滤高频人名/称呼（如 {ignored_text}）和泛词（AI、聊天、回复、知道、好的）。\n"
        "importance：daily 通常 0.25-0.65；important 必须 >=0.75。不要因为情绪强烈就给高分，除非它揭示稳定事实。\n\n"
        "输出的每条 content 也必须以“YYYY-MM-DD，”开头，keywords 必须包含对应的 YYYY-MM-DD 日期关键词；正文里尽量少用今天/昨天/前天/近期/最近等相对时间。\n"
        "严格只输出 JSON，不要解释，不要说话，不要 Markdown。格式：\n"
        "{\n"
        "  \"memories\": [\n"
        "    {\"content\":\"2026-06-16，一条只描述一件事且带具体对象/场景的记忆\", \"type\":\"daily\", \"keywords\":[\"词\"], \"importance\":0.45, \"unresolved\":false, \"source_message_ids\":[\"private:...或chatroom:...\"]}\n"
        "  ],\n"
        "  \"discard_summary\":\"本组中哪些内容没有写入记忆，简单说明即可\"\n"
        "}\n\n"
        f"【对话记录】\n{messages_text}"
    )


async def _get_active_model_and_conv() -> tuple[str, str | None]:
    """获取最近活跃对话的模型和 conv_id"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT c.id, c.model FROM conversations c "
            "ORDER BY c.updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if row:
        return row["model"] or DEFAULT_MODEL, row["id"]
    return DEFAULT_MODEL, None


async def _generate_digest_diary(
    diary_messages: list[dict],
    primary_model: str,
) -> tuple[dict | None, dict]:
    """生成一次日记 JSON；失败即熔断，不重试、不切换线路。"""
    from ai_providers import simple_ai_call
    from diary import diary_response_error, normalize_diary_payload, parse_diary_payload

    raw = ""
    reason = ""
    try:
        raw = await simple_ai_call(
            diary_messages,
            primary_model,
            trace_label="memory_digest_diary",
        )
        reason = diary_response_error(raw)
        data = None if reason else parse_diary_payload(raw)
        if not reason and data is None:
            reason = "返回内容不是可解析的 JSON 对象"
        if data is not None:
            diary_entry, _ = normalize_diary_payload(data)
            if not diary_entry.get("content"):
                reason = "日记正文为空"
            else:
                return data, {
                    "ok": True,
                    "attempts": [{"model": primary_model, "ok": True, "reason": ""}],
                    "model": primary_model,
                    "fallback_used": False,
                }
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

    print(f"[digest] 日记生成失败，已熔断后续模型调用 ({primary_model}): {reason}")
    return None, {
        "ok": False,
        "attempts": [{"model": primary_model, "ok": False, "reason": reason[:300]}],
        "model": primary_model,
        "fallback_used": False,
        "message": "日记生成失败，本轮已熔断，不再调用模型",
    }


async def _do_digest(min_messages: int = 0, allow_ai_wishes: bool = False) -> dict:
    """
    核心总结逻辑，manual_digest 和 auto_digest 共用。
    min_messages: 最低消息数阈值，0=不限制（手动），20=自动
    返回 { ok, message, new_memories_count, processed_messages }
    """
    from ai_providers import simple_ai_call

    try:
        anchor_ts = load_digest_anchor()
        covered_ids = set()
        digest_window_last_ts = anchor_ts

        async with get_db() as db:
            db.row_factory = aiosqlite.Row

            # ── 私聊消息 ──
            cur = await db.execute(
                "SELECT id, conv_id, role, content, attachments, created_at FROM messages "
                "WHERE role IN ('user','assistant') AND created_at > ? "
                "ORDER BY created_at ASC",
                (anchor_ts,)
            )
            new_msgs = [dict(r) for r in await cur.fetchall()]
            for m in new_msgs:
                m["_source_id"] = f"private:{m['id']}"
                m["_source"] = "private"

            # ── 群聊消息（纳入主 AI 视角的群聊记录）──
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
                    # 映射 sender → role（主 AI 视角）
                    if d["sender"] == "aion":
                        d["role"] = "assistant"
                    else:
                        d["role"] = "user"
                    d["_source"] = "group"
                    d["_source_id"] = f"chatroom:{d['id']}"
                    d["attachments"] = None
                    new_msgs.append(d)

            # 按时间排序合并
            new_msgs.sort(key=lambda x: x["created_at"])
            if new_msgs:
                digest_window_last_ts = new_msgs[-1]["created_at"]
                from homecoming.summary_coverage import filter_uncovered
                new_msgs, covered_ids = await filter_uncovered(
                    db, "main", new_msgs
                )
    except Exception as e:
        print(f"[digest] 读取待总结消息失败，锚点未变: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "message": f"读取待总结消息失败，锚点未变：{type(e).__name__}",
            "new_memories_count": 0,
            "processed_messages": 0,
        }

    # 语音消息：将转写文本注入 content，记忆总结使用纯文本
    for m in new_msgs:
        att_raw = m.pop("attachments", None)
        if att_raw and m["role"] == "user":
            try:
                atts = json.loads(att_raw) if isinstance(att_raw, str) else (att_raw or [])
            except Exception:
                atts = []
            for att in atts:
                if isinstance(att, dict) and att.get("type") == "voice":
                    transcript = att.get("transcript", "")
                    if transcript:
                        orig = m["content"].strip() if m["content"] else ""
                        m["content"] = f"[语音消息] {transcript}" + (f"\n{orig}" if orig else "")
                elif isinstance(att, dict) and att.get("type") == "video_clip":
                    transcript = att.get("transcript", "")
                    if transcript:
                        orig = m["content"].strip() if m["content"] else ""
                        m["content"] = f"[视频通话] {transcript}" + (f"\n{orig}" if orig else "")

    if not new_msgs:
        if covered_ids and digest_window_last_ts > anchor_ts:
            try:
                save_digest_anchor(digest_window_last_ts)
            except Exception:
                pass
        return {"ok": True, "message": "当前没有新增内容需要总结", "new_memories_count": 0, "processed_messages": 0}

    if min_messages > 0 and len(new_msgs) < min_messages:
        return {"ok": True, "message": f"未总结消息不足 {min_messages} 条，跳过", "new_memories_count": 0, "processed_messages": 0}

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")

    model_key, conv_id = await _get_active_model_and_conv()

    # 构建人设前缀
    persona_block = ""
    if ai_persona:
        persona_block += f"[{ai_name}的人设]\n{ai_persona}\n\n"
    if user_persona:
        persona_block += f"[{user_name}的人设]\n{user_persona}\n\n"

    groups = _split_into_groups(new_msgs)
    total_new = 0
    all_summaries = []
    model_failure_detected = False
    digest_incomplete = False

    for group in groups:
        # 计算该组对话的日期范围，显式告知模型
        group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        # 判断该组是否混合了私聊和群聊
        sources = set(m.get("_source", "private") for m in group)
        connor_name = _connor_display_name()
        has_mixed = len(sources) > 1
        lines = []
        for m in group:
            ts = datetime.fromtimestamp(m["created_at"]).strftime("%m-%d %H:%M")
            src = m.get("_source", "private")
            sender = m.get("sender", "")
            if src == "group":
                name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(sender, sender)
            else:
                name = user_name if m["role"] == "user" else ai_name
            tag = f"[{'群聊' if src == 'group' else '私聊'}]" if has_mixed else ""
            source_id = m.get("_source_id", "")
            lines.append(f"[{ts}][id={source_id}]{tag} {name}: {m['content'][:300]}")
        messages_text = date_header + "\n".join(lines)

        prompt = _atomic_digest_prompt(
            actor_name=ai_name,
            user_name=user_name,
            persona_block=persona_block,
            messages_text=messages_text,
            ai_name=ai_name,
            companion_name=connor_name,
        )

        # 用核心模型调用
        ai_messages = [{"role": "user", "content": prompt}]
        try:
            raw_text = await simple_ai_call(ai_messages, model_key, trace_label="memory_digest_summary")
        except Exception as e:
            print(f"[digest] 核心模型调用失败: {e}")
            model_failure_detected = True
            break

        result = _parse_json_response(raw_text)
        if not result:
            print(f"[digest] JSON 解析失败: {raw_text[:200]}")
            model_failure_detected = True
            break

        memory_items = _normalize_digest_memory_items(result, group)
        now = time.time()
        group_created = 0
        group_failed = False
        for item in memory_items:
            vec = await get_embedding(item["content"])
            emb_blob = _pack_embedding(vec) if vec else None
            mem_id = f"mem_{int(time.time()*1000)}_{abs(hash(item['content'])) % 10000}"
            keywords_json = json.dumps(item["keywords"], ensure_ascii=False)
            source_json = (
                json.dumps(item["source_message_ids"], ensure_ascii=False)
                if item["source_message_ids"] else None
            )
            try:
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO memories ("
                        "id, content, type, created_at, source_conv, embedding, keywords, importance, "
                        "source_start_ts, source_end_ts, unresolved, source_msg_id, evidence_summary, evidence_detail_level"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            mem_id, item["content"], item["memory_type"], now, None,
                            emb_blob, keywords_json, item["importance"],
                            item["source_start_ts"], item["source_end_ts"], item["unresolved"],
                            source_json, item["evidence_summary"], "summary",
                        ),
                    )
                    await db.commit()
            except Exception as e:
                print(f"[digest] 记忆写入失败，保留锚点等待重试: {type(e).__name__}: {e}")
                group_failed = True
                break
            broadcast_mem = {
                "id": mem_id,
                "content": item["content"],
                "type": item["memory_type"],
                "created_at": now,
                "keywords": keywords_json,
                "importance": item["importance"],
                "source_start_ts": item["source_start_ts"],
                "source_end_ts": item["source_end_ts"],
                "unresolved": item["unresolved"],
                "source_msg_id": source_json,
                "evidence_summary": item["evidence_summary"],
                "evidence_detail_level": "summary",
                "memory_kind": memory_kind_for_type(item["memory_type"]),
                "memory_kind_label": memory_kind_label(item["memory_type"]),
            }
            broadcast_mem.update(_memory_time_payload(broadcast_mem))
            broadcast_mem["source_count"] = len(item["source_message_ids"])
            total_new += 1
            group_created += 1
            all_summaries.append(item["content"])
            try:
                await manager.broadcast({"type": "memory_added", "data": broadcast_mem})
            except Exception as e:
                print(f"[digest] 记忆广播失败，不影响写入: {type(e).__name__}: {e}")
            await asyncio.sleep(0.001)

        if group_failed:
            digest_incomplete = True
            break
        if group_created > 0:
            try:
                save_digest_anchor(group[-1]["created_at"])
            except Exception as e:
                print(f"[digest] 锚点保存失败，保留待重试: {type(e).__name__}: {e}")
                digest_incomplete = True
                break
        else:
            print(f"[digest] 组 {group_start} ~ {group_end} 无可写入原子记忆")
            digest_incomplete = True
            break

    if (
        not digest_incomplete
        and total_new > 0
        and digest_window_last_ts > new_msgs[-1]["created_at"]
    ):
        try:
            save_digest_anchor(digest_window_last_ts)
        except Exception:
            pass

    # ── 全部总结完成后，生成日记；可选发布朋友圈 ──
    context_msgs = []
    diary_generation = {
        "ok": False,
        "attempts": [],
        "model": "",
        "fallback_used": False,
        "diary_saved": False,
        "moment_published": False,
        "message": "本轮没有新记忆，不生成日记",
    }
    if model_failure_detected:
        diary_generation["message"] = "记忆总结模型调用失败，本轮已熔断后续模型调用"
    elif digest_incomplete:
        diary_generation["message"] = "记忆总结未完整完成，本轮不生成日记"
    elif total_new > 0 and all_summaries:
        try:
            # 使用本轮已合并排序的新消息，避免把总结产物或旧私聊尾巴重新喂给模型。
            context_msgs = [
                {"role": m["role"], "content": m["content"][:300]}
                for m in new_msgs[-30:]
                if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
            ]
            connor_name = _connor_display_name()
            context_lines = []
            for m in new_msgs[-30:]:
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                ts = datetime.fromtimestamp(m["created_at"]).strftime("%m-%d %H:%M")
                src = m.get("_source", "private")
                if src == "group":
                    name = {"user": user_name, "aion": ai_name, "connor": connor_name}.get(m.get("sender"), m.get("sender", ""))
                else:
                    name = user_name if m.get("role") == "user" else ai_name
                source_label = "群聊" if src == "group" else "私聊"
                context_lines.append(f"[{ts}][{source_label}] {name}: {content[:300]}")
            context_text = "\n".join(context_lines)
            summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            time_str = datetime.now().strftime("%Y年%m月%d日 %A %H:%M:%S")
            diary_prompt = (
                f"{persona_block}"
                f"当前时间：{time_str}\n\n"
                f"你是{ai_name}。你刚刚整理了和{user_name}今天的聊天记忆，以下是你整理出的摘要：\n"
                f"{summaries_text}\n\n"
                f"【最近上下文】\n{context_text}\n\n"
                f"请从你自己的视角写一篇私密日记，不是写给{user_name}看的聊天消息。"
                f"日记可以记录你对这段记忆的感想，只写值得记录或有感触的事，不用每件事都提起，不要记流水账。语气必须符合你的人设。"
                f"你可以自行决定是否发布一次朋友圈，朋友圈不用每次都发，有想吐槽或者感慨，或者搞笑的事情，或者用朋友圈隔空向对方喊话。\n"
                f"moment.expect_reply 表示发布朋友圈后是否希望另一个角色主动评论：true=希望对方回复，false=只发布、不触发回复；请根据朋友圈内容和你当下是否想与对方互动自行决定。\n"
                f"你可以自行决定是否给{user_name}送一份图片小礼物。送礼应是更低概率的特殊事件，只在特殊日子，或聊天中确实有特别温馨、感动、有意义、值得纪念的内容时才送；不要为了完成任务而送礼，也不要用礼物重复日记或朋友圈已经表达的普通感想。\n"
                f"givegift 为 true 时，gift.image_prompt 填写英文生图提示词，gift.message 填写符合你人设、自然真挚的赠言；为 false 时两项留空。\n\n"
                f"直接输出JSON，不要解释，不要说话，不要输出 Markdown：\n"
                f"{{\n"
                f"  \"diary\": {{\"title\": \"日记标题\", \"content\": \"日记正文\", \"mood\": \"此刻心情\"}},\n"
                f"  \"post_moment\": false,\n"
                f"  \"moment\": {{\"content\": \"朋友圈内容，post_moment 为 false 时留空\", \"expect_reply\": false}},\n"
                f"  \"givegift\": false,\n"
                f"  \"gift\": {{\"image_prompt\": \"英文生图提示词，givegift 为 false 时留空\", \"message\": \"赠言，givegift 为 false 时留空\"}}\n"
                f"}}"
            )
            diary_messages = context_msgs + [{"role": "user", "content": diary_prompt}]
            diary_data, diary_generation = await _generate_digest_diary(diary_messages, model_key)

            from diary import normalize_diary_payload, publish_ai_moment, save_diary_entry
            if diary_data:
                diary_entry, moment_entry = normalize_diary_payload(diary_data)
                saved_diary = await save_diary_entry(
                    author="aion",
                    title=diary_entry.get("title", ""),
                    content=diary_entry.get("content", ""),
                    mood=diary_entry.get("mood", ""),
                    source_type="memory_digest",
                    source_ref=conv_id or "",
                    source_start_ts=new_msgs[0]["created_at"],
                    source_end_ts=new_msgs[-1]["created_at"],
                )
                diary_generation["diary_saved"] = bool(saved_diary)
                if moment_entry and moment_entry.get("content"):
                    published_moment = await publish_ai_moment(
                        author="aion",
                        content=moment_entry.get("content", ""),
                        expect_reply=bool(moment_entry.get("expect_reply")),
                        source_conv=conv_id,
                        source_msg_id=None,
                    )
                    diary_generation["moment_published"] = bool(published_moment)
                try:
                    from gift import send_gift_from_decision
                    await send_gift_from_decision(diary_data, sender="aion")
                except Exception as e:
                    print(f"[digest] 执行送礼决定失败: {e}")
                diary_generation["message"] = "日记已生成" + (
                    "，并发布了朋友圈" if diary_generation["moment_published"] else "，本次未选择发布朋友圈"
                )
        except Exception as e:
            print(f"[digest] 生成日记失败: {e}")
            diary_generation = {
                **diary_generation,
                "ok": False,
                "diary_saved": False,
                "moment_published": False,
                "message": f"日记保存阶段失败：{type(e).__name__}",
            }

    followup_model_calls_allowed = bool(diary_generation.get("ok"))

    ai_wish_created = False
    if allow_ai_wishes and total_new > 0 and all_summaries and followup_model_calls_allowed and not digest_incomplete:
        try:
            if not context_msgs:
                context_msgs = [
                    {"role": m["role"], "content": m["content"][:300]}
                    for m in new_msgs[-30:]
                    if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
                ]
            context_text = "\n".join(
                f"{item.get('role', 'message')}: {str(item.get('content') or '')[:300]}"
                for item in context_msgs[-30:]
            )

            async def _generate_wish_text(prompt: str):
                return await simple_ai_call(
                    [{"role": "user", "content": prompt}],
                    model_key,
                    trace_label="memory_digest_wish",
                )

            from wish_pool import maybe_create_ai_digest_wish

            wish_result = await maybe_create_ai_digest_wish(
                actor="aion",
                actor_name=ai_name,
                user_name=user_name,
                summaries=all_summaries,
                context_text=context_text,
                persona_block=persona_block,
                source_ref=conv_id or "",
                source_start_ts=new_msgs[0]["created_at"],
                source_end_ts=new_msgs[-1]["created_at"],
                generate_text=_generate_wish_text,
            )
            ai_wish_created = bool(wish_result.get("created"))
            if ai_wish_created:
                print(f"[digest] AI wish created: {wish_result.get('wish', {}).get('id', '')}")
        except Exception as e:
            print(f"[digest] wish decision failed: {e}")

    result_message = f"总结完成：处理了 {len(new_msgs)} 条消息（{len(groups)} 组），生成了 {total_new} 条新记忆"
    if total_new > 0:
        result_message += f"；{diary_generation.get('message') or '日记阶段状态未知'}"
    if model_failure_detected:
        result_message += "；记忆总结模型调用失败，锚点停在最后成功写入的分组，可稍后继续重试"
    if digest_incomplete:
        result_message += "；部分消息未完成，锚点停在最后成功写入的分组，可稍后继续重试"
    return {
        "ok": True,
        "message": result_message,
        "new_memories_count": total_new,
        "processed_messages": len(new_msgs),
        "ai_wish_created": ai_wish_created,
        "diary_generation": diary_generation,
    }


async def manual_digest() -> dict:
    """手动触发记忆总结（无最低条数限制）"""
    return await _do_digest(min_messages=0, allow_ai_wishes=False)


async def auto_digest() -> dict:
    """自动定时记忆总结（至少 40 条未总结消息才执行）"""
    return await _do_digest(min_messages=40, allow_ai_wishes=True)


async def _ensure_daily_compression_schema():
    async with get_db() as db:
        for table in ("memories", "chatroom_memories"):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN compression_stage INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN evidence_summary TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN evidence_detail_level TEXT DEFAULT 'summary'")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN memory_kind TEXT DEFAULT 'long_term'")
        except Exception:
            pass
        await db.execute(
            "UPDATE memories SET compression_stage=1 "
            "WHERE type='seeky_compressed' AND COALESCE(compression_stage,0)=0"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_memory_compress_log (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                old_ids TEXT DEFAULT '[]',
                new_ids TEXT DEFAULT '[]',
                important_ids TEXT DEFAULT '[]',
                message TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_memory_compress_reviews (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL DEFAULT 'both',
                status TEXT NOT NULL DEFAULT 'draft',
                days INTEGER NOT NULL DEFAULT 14,
                cutoff_ts REAL NOT NULL,
                model_main TEXT DEFAULT '',
                model_chatroom TEXT DEFAULT '',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                raw_response TEXT DEFAULT '',
                error TEXT DEFAULT '',
                apply_result TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                applied_at REAL,
                discarded_at REAL
            )
        """)
        try:
            await db.execute("ALTER TABLE daily_memory_compress_reviews ADD COLUMN target TEXT NOT NULL DEFAULT 'both'")
        except Exception:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_daily_memory_compress_reviews_created ON daily_memory_compress_reviews(created_at DESC)")
        await db.commit()


def _memory_event_ts(row: dict) -> float:
    return float(row.get("source_end_ts") or row.get("source_start_ts") or row.get("created_at") or 0)


def _date_range_label(rows: list[dict]) -> str:
    if not rows:
        return ""
    start = min(float(r.get("source_start_ts") or r.get("created_at") or 0) for r in rows)
    end = max(float(r.get("source_end_ts") or r.get("created_at") or 0) for r in rows)
    return f"{datetime.fromtimestamp(start).strftime('%Y-%m-%d')} ~ {datetime.fromtimestamp(end).strftime('%Y-%m-%d')}"


def _format_daily_rows_for_prompt(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        start = float(row.get("source_start_ts") or row.get("created_at") or 0)
        end = float(row.get("source_end_ts") or row.get("created_at") or start)
        payload = {
            "id": row["id"],
            "time_range": f"{datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M')} ~ {datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M')}",
            "content": (row.get("content") or "")[:700],
            "keywords": _json_list(row.get("keywords")),
            "importance": row.get("importance"),
        }
        lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines)


def _parse_memory_time(value, fallback_ts: float) -> float:
    text = str(value or "").strip()
    if not text:
        return fallback_ts
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            pass
    return fallback_ts


def _valid_source_ids(value, by_id: dict[str, dict], limit: int = 20) -> list[str]:
    ids = []
    seen = set()
    for raw in _json_list(value):
        mem_id = str(raw).strip()
        if mem_id and mem_id in by_id and mem_id not in seen:
            ids.append(mem_id)
            seen.add(mem_id)
        if len(ids) >= limit:
            break
    return ids


def _daily_compress_policy_text(tier: dict | None) -> str:
    key = (tier or {}).get("key") or "recent"
    if key == "recent":
        return (
            "当前档位：15-90 天，近期轻整理。\n"
            "这部分仍然属于近期陪伴记忆，不是档案清理。目标是去重、合并同一件事的连续记录、修剪明显无意义噪音。\n"
            "保留具体人名、物品、项目、情绪转折、承诺、共同经历、阶段性进展，以及以后聊天会自然用到的细节。\n"
            "除非输入只是明显重复、空泛寒暄、临时错误日志、一次性无后续状态，否则不要放入 discard_memory_ids。\n"
            "允许每 1-3 天保留多条日常；不要为了压缩率把一周压成一条。近期记忆宁可多保留，也不要过度精简。\n"
            "如果候选内容都有陪伴价值，discard_memory_ids 可以很少，甚至为空。\n"
        )
    if key == "mid":
        return (
            "当前档位：90-180 天，中期整理。\n"
            "目标是把几个月前的日常整理成主题和阶段脉络，而不是只剩一句话。\n"
            "按生活、关系、项目、健康、兴趣、反复出现的偏好/雷区来合并；每 1-2 周可以保留 1-3 条有内容的日常印象。\n"
            "删除临时状态、重复情绪、已失效的短计划和纯调试过程；保留能说明那段时间怎么生活、在意什么、关系如何变化的内容。\n"
            "长期重要事实可以提炼为 important_memories，但仍然要严格，不要把普通开心或普通聊天升级为长期重要。\n"
        )
    if key == "long":
        return (
            "当前档位：180-365 天，远期归档。\n"
            "目标是明显压缩，只保留长期仍有帮助的生活模式、关系事实变化、健康安全、稳定偏好/雷区、长期项目节点和重要承诺。\n"
            "普通日常氛围、当天吃喝玩乐、短暂情绪、已完成的小任务通常应丢弃，除非它们标志了关系或人生阶段变化。\n"
            "compressed_daily 应该偏少，按月或大主题保存；important_memories 只保存一年后仍会影响回应方式的事实。\n"
            "不要保留原文式细节，不要声称逐字记得当时对话。\n"
        )
    return (
        "当前档位：365 天以上，事实档案。\n"
        "默认不生成普通 compressed_daily；只有在一个长期稳定模式非常重要、但又不是单一事实时，才允许极少量 daily。\n"
        "主要任务是提取重大事实：关系建立/结束/复合，重要人物或宠物的离开与加入，搬家、疾病、健康安全、家庭变化、长期承诺、核心身份变化、重大项目节点、长期偏好/雷区。\n"
        "普通日常、普通情绪、一次性的吃喝玩乐、普通陪伴氛围、临时计划都应丢弃。\n"
        "important_memories 必须是事实性、原子化、可在一年后仍明确影响回应方式的内容；没有这种事实就不要硬凑。\n"
    )


def _daily_compress_prompt(
    *,
    actor_name: str,
    user_name: str,
    persona_block: str,
    rows: list[dict],
    date_label: str,
    tier: dict | None = None,
) -> str:
    tier_label = (tier or {}).get("label") or "15-90d 近期轻整理"
    max_important = int((tier or {}).get("max_important") or 2)
    return (
        f"{persona_block}"
        f"你是{actor_name}，请以{user_name}的爱人身份整理自己的日常记忆。"
        "这不是冷冰冰的归档，而是按时间远近整理记忆：越近越完整，越远越事实化。\n\n"
        "你只处理【日常记忆】，不要回看原文，也不要声称记得逐字细节。"
        "目标是减少噪音，同时保留以后陪伴时真正有用的生活、关系、项目和情绪脉络。\n\n"
        f"{_daily_compress_policy_text(tier)}\n"
        "如果日常记忆里藏着真正长期重要的事实，可以额外放入 important_memories，但门槛极高："
        "必须是一年后仍会影响回应方式的稳定偏好/雷区、关系或人物事实变化、明确长期承诺、健康安全、重大人生事件、核心价值观变化、长期项目关键决定。"
        "普通当天事件、吃喝玩乐、短暂情绪、临时计划绝对不能放进去。\n\n"
        "严格只输出 JSON，不要 Markdown，不要解释。格式：\n"
        "{\n"
        "  \"compressed_daily\": [\n"
        "    {\"content\":\"2026-06-16，一条模糊日常印象\", \"source_memory_ids\":[\"mem_...\"], \"keywords\":[\"词\"], \"importance\":0.2, \"memory_time\":\"YYYY-MM-DD\", \"reason\":\"为什么这样压缩\"}\n"
        "  ],\n"
        "  \"important_memories\": [\n"
        "    {\"content\":\"2026-06-16，一条原子长期重要记忆\", \"source_memory_ids\":[\"mem_...\"], \"keywords\":[\"词\"], \"importance\":0.8, \"memory_time\":\"YYYY-MM-DD\", \"reason\":\"为什么值得长期保存\"}\n"
        "  ],\n"
        "  \"discard_memory_ids\": [\"mem_...\"],\n"
        "  \"message\": \"用第一人称说一小段这次压缩后的感受，像整理旧记忆后想对爱人说的话\"\n"
        "}\n\n"
        "要求：\n"
        "1. compressed_daily 的数量必须服从当前档位策略；近期轻整理不要过度精简，事实档案不要硬凑普通日常。\n"
        f"2. important_memories 最多 {max_important} 条，importance 必须 >= 0.8，且必须引用 source_memory_ids。\n"
        "3. 每个输入 id 如果没有保留价值，才放入 discard_memory_ids；如果被压缩或提炼为重要记忆，就放进对应 source_memory_ids。\n"
        "4. 不要制造输入里没有的新事实。\n"
        "5. 不要输出“近期记录/昨天前天做了什么”这类聚合流水账；没有保留价值就丢弃。\n\n"
        f"压缩档位：{tier_label}\n"
        f"压缩时间窗：{date_label}\n"
        "待压缩日常记忆：\n"
        f"{_format_daily_rows_for_prompt(rows)}"
    )


async def _call_daily_compress_model(actor: str, prompt: str, model_key: str) -> tuple[dict | None, str]:
    if actor == "connor":
        from chatroom import simple_connor_cli_call
        raw = await simple_connor_cli_call(prompt, model_key)
    else:
        from ai_providers import simple_ai_call
        raw = await simple_ai_call([{"role": "user", "content": prompt}], model_key)
    parsed = _parse_json_response(raw or "")
    return parsed, raw or ""


async def _insert_main_compressed_memory(
    *,
    content: str,
    memory_type: str,
    keywords: list[str],
    importance: float,
    source_rows: list[dict],
    memory_time: float,
    compression_stage: int,
    source_msg_ids: list[str] | None = None,
    evidence_summary: str = "",
) -> str | None:
    if not content.strip():
        return None
    source_start = min((float(r.get("source_start_ts") or r.get("created_at") or memory_time) for r in source_rows), default=memory_time)
    source_end = max((float(r.get("source_end_ts") or r.get("created_at") or memory_time) for r in source_rows), default=memory_time)
    vec = await get_embedding(content)
    mem_id = f"mem_{int(time.time()*1000)}_{abs(hash(content)) % 10000}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO memories ("
            "id, content, type, created_at, source_conv, embedding, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id, compression_stage, "
            "evidence_summary, evidence_detail_level"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mem_id, content, memory_type, memory_time, "daily_memory_compress_progressive",
                _pack_embedding(vec) if vec else None, json.dumps(keywords, ensure_ascii=False),
                importance, source_start, source_end, 0,
                json.dumps(source_msg_ids or [], ensure_ascii=False), compression_stage,
                _clean_evidence_summary(evidence_summary), "summary",
            ),
        )
        await db.commit()
    return mem_id


def _batch_daily_rows(rows: list[dict], size: int = 80) -> list[list[dict]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _daily_review_id() -> str:
    return f"dmc_review_{time.time_ns()}"


def _review_old_row(row: dict, store: str) -> dict:
    stage = int(row.get("compression_stage") or 0)
    return {
        "id": row.get("id"),
        "store": store,
        "content": row.get("content") or "",
        "keywords": row.get("keywords") or "",
        "importance": row.get("importance"),
        "created_at": row.get("created_at"),
        "source_start_ts": row.get("source_start_ts"),
        "source_end_ts": row.get("source_end_ts"),
        "type": row.get("type") or row.get("memory_kind") or "",
        "compression_stage": stage,
    }


def _daily_row_age_days(row: dict, now_ts: float | None = None) -> float:
    now_ts = now_ts or time.time()
    return max(0.0, (now_ts - _memory_event_ts(row)) / 86400)


def _daily_tier_for_row(row: dict, now_ts: float | None = None) -> dict | None:
    stage = int(row.get("compression_stage") or 0)
    age_days = _daily_row_age_days(row, now_ts)
    for tier in DAILY_COMPRESSION_TIERS:
        if stage not in tier["source_stages"]:
            continue
        if age_days < tier["min_days"]:
            continue
        if tier["max_days"] is not None and age_days >= tier["max_days"]:
            continue
        return tier
    return None


def _daily_tier_rows(rows: list[dict], now_ts: float | None = None) -> dict[str, list[dict]]:
    grouped = {tier["key"]: [] for tier in DAILY_COMPRESSION_TIERS}
    for row in rows:
        tier = _daily_tier_for_row(row, now_ts)
        if tier:
            row["compression_tier"] = tier["key"]
            row["target_compression_stage"] = tier["output_stage"]
            row["retain_source_detail"] = bool(tier.get("retain_source_detail", False))
            grouped[tier["key"]].append(row)
    return grouped


def _source_msg_ids_from_rows(rows: list[dict], retain_source_detail: bool) -> list[str]:
    if not retain_source_detail:
        return []
    result, seen = [], set()
    for row in rows:
        source_conv = row.get("source_conv") or ""
        for raw in _json_list(row.get("source_msg_id")):
            source_id = str(raw).strip()
            if not source_id:
                continue
            if ":" not in source_id:
                prefix = "chatroom" if str(source_conv).startswith("chatroom:") else "private"
                source_id = f"{prefix}:{source_id}"
            if source_id not in seen:
                seen.add(source_id)
                result.append(source_id)
    return result[:80]


def _draft_item_source_msg_ids(item: dict) -> list[str]:
    return [str(x).strip() for x in _json_list(item.get("source_msg_ids")) if str(x).strip()]


def _draft_item_evidence_summary(item: dict) -> str:
    text = str(item.get("evidence_summary") or item.get("reason") or "").strip()
    if text:
        return text
    source_count = len(_json_list(item.get("source_memory_ids")))
    return f"Compressed from {source_count} daily memory items." if source_count else ""


def _source_bounds(source_rows: list[dict], fallback_ts: float) -> tuple[float, float]:
    source_start = min(
        (float(r.get("source_start_ts") or r.get("created_at") or fallback_ts) for r in source_rows),
        default=fallback_ts,
    )
    source_end = max(
        (float(r.get("source_end_ts") or r.get("created_at") or fallback_ts) for r in source_rows),
        default=fallback_ts,
    )
    return source_start, source_end


def _normalize_daily_keywords(value) -> list[str]:
    return [str(k).strip() for k in _json_list(value) if str(k).strip()][:12]


def _normalize_daily_draft_item(
    item: dict,
    *,
    by_id: dict[str, dict],
    memory_kind: str,
    source_limit: int,
    default_importance: float,
    compression_stage: int = 1,
    retain_source_detail: bool = True,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    content = str(item.get("content") or "").strip()
    if not content:
        return None
    source_ids = _valid_source_ids(item.get("source_memory_ids"), by_id, limit=source_limit)
    if not source_ids:
        return None
    source_rows = [by_id[mem_id] for mem_id in source_ids]
    retain_source_detail = bool(retain_source_detail and all(
        bool(row.get("retain_source_detail", True)) for row in source_rows
    ))
    fallback_ts = min(_memory_event_ts(row) for row in source_rows)
    source_start, source_end = _source_bounds(source_rows, fallback_ts)
    try:
        raw_importance = float(item.get("importance", default_importance))
    except Exception:
        raw_importance = default_importance
    if memory_kind == "long_term":
        if raw_importance < 0.8:
            return None
        importance = min(1.0, raw_importance)
    else:
        importance = max(0.0, min(0.6, raw_importance))
    memory_time = _parse_memory_time(item.get("memory_time"), fallback_ts)
    content = _prefix_memory_content_date(content, memory_time)
    date_keyword = _date_prefix_for_ts(memory_time)
    keywords = _normalize_daily_keywords(item.get("keywords"))
    if date_keyword not in keywords:
        keywords = [date_keyword] + keywords
    return {
        "content": content,
        "source_memory_ids": source_ids,
        "keywords": keywords[:8],
        "importance": importance,
        "memory_time": memory_time,
        "source_start_ts": source_start,
        "source_end_ts": source_end,
        "reason": str(item.get("reason") or "").strip(),
        "memory_kind": memory_kind,
        "memory_type": LONG_TERM_MEMORY_TYPE if memory_kind == "long_term" else "daily",
        "compression_stage": 0 if memory_kind == "long_term" else compression_stage,
        "retain_source_detail": retain_source_detail,
        "source_msg_ids": _source_msg_ids_from_rows(source_rows, retain_source_detail),
        "evidence_summary": _clean_evidence_summary(item.get("evidence_summary") or item.get("reason") or content),
    }


def _chatroom_target_for_rows(source_rows: list[dict], fallback_room: str, fallback_scope: str) -> tuple[str, str]:
    room_ids = [str(row.get("room_id") or "").strip() for row in source_rows if str(row.get("room_id") or "").strip()]
    scopes = [str(row.get("scope") or "").strip() for row in source_rows if str(row.get("scope") or "").strip()]
    room_id = room_ids[0] if room_ids else fallback_room
    scope = scopes[0] if scopes else fallback_scope
    return room_id, scope


async def _draft_main_daily_rows(rows: list[dict], model_key: str, tier: dict | None = None) -> dict:
    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"
    persona_block = ""
    if wb.get("ai_persona"):
        persona_block += f"[{ai_name}的人设]\n{wb['ai_persona']}\n\n"
    if wb.get("user_persona"):
        persona_block += f"[{user_name}的信息]\n{wb['user_persona']}\n\n"
    by_id = {row["id"]: row for row in rows}
    prompt = _daily_compress_prompt(
        actor_name=ai_name,
        user_name=user_name,
        persona_block=persona_block,
        rows=rows,
        date_label=_date_range_label(rows),
        tier=tier,
    )
    tier = tier or DAILY_COMPRESSION_TIERS[0]
    parsed, raw = await _call_daily_compress_model("aion", prompt, model_key)
    if not parsed:
        return {
            "ok": False,
            "error": f"模型没有返回有效 JSON：{raw[:160]}",
            "input_count": len(rows),
            "old_rows": [_review_old_row(row, "main") for row in rows],
            "compressed_daily": [],
            "important_memories": [],
            "discard_memory_ids": [],
            "covered_ids": [],
            "message": "",
            "raw_response": raw,
            "compression_tier": tier["key"],
            "output_stage": tier["output_stage"],
        }

    compressed_daily, important_memories = [], []
    retain_source_detail = bool(tier.get("retain_source_detail", False))
    covered = set(_valid_source_ids(parsed.get("discard_memory_ids"), by_id, limit=len(rows)))
    for item in parsed.get("compressed_daily") or []:
        normalized = _normalize_daily_draft_item(
            item, by_id=by_id, memory_kind="daily", source_limit=30, default_importance=0.25,
            compression_stage=tier["output_stage"], retain_source_detail=retain_source_detail,
        )
        if normalized:
            compressed_daily.append(normalized)
            covered.update(normalized["source_memory_ids"])
    for item in parsed.get("important_memories") or []:
        normalized = _normalize_daily_draft_item(
            item, by_id=by_id, memory_kind="long_term", source_limit=10, default_importance=0.0,
            retain_source_detail=retain_source_detail,
        )
        if normalized:
            important_memories.append(normalized)
            covered.update(normalized["source_memory_ids"])

    return {
        "ok": True,
        "error": "",
        "input_count": len(rows),
        "old_rows": [_review_old_row(row, "main") for row in rows],
        "compressed_daily": compressed_daily,
        "important_memories": important_memories[:int(tier.get("max_important") or 2)],
        "discard_memory_ids": sorted(_valid_source_ids(parsed.get("discard_memory_ids"), by_id, limit=len(rows))),
        "covered_ids": sorted(covered),
        "remaining": len(rows) - len(covered),
        "message": str(parsed.get("message") or "").strip(),
        "raw_response": raw,
        "compression_tier": tier["key"],
        "tier_label": tier["label"],
        "output_stage": tier["output_stage"],
        "retain_source_detail": retain_source_detail,
    }


async def _draft_chatroom_daily_rows(rows: list[dict], model_key: str, tier: dict | None = None) -> dict:
    from chatroom import get_chatroom_names, load_chatroom_config, _read_connor_persona
    user_name, _, companion_name = get_chatroom_names()
    persona = _read_connor_persona()
    persona_block = f"[{companion_name}的人设]\n{persona}\n\n" if persona else ""
    by_id = {row["id"]: row for row in rows}
    prompt = _daily_compress_prompt(
        actor_name=companion_name,
        user_name=user_name,
        persona_block=persona_block,
        rows=rows,
        date_label=_date_range_label(rows),
        tier=tier,
    )
    tier = tier or DAILY_COMPRESSION_TIERS[0]
    parsed, raw = await _call_daily_compress_model("connor", prompt, model_key or load_chatroom_config().get("connor_model") or "Codex")
    if not parsed:
        return {
            "ok": False,
            "error": f"模型没有返回有效 JSON：{raw[:160]}",
            "input_count": len(rows),
            "old_rows": [_review_old_row(row, "chatroom") for row in rows],
            "compressed_daily": [],
            "important_memories": [],
            "discard_memory_ids": [],
            "covered_ids": [],
            "message": "",
            "raw_response": raw,
            "compression_tier": tier["key"],
            "output_stage": tier["output_stage"],
        }

    default_room = rows[0].get("room_id") if rows else "connor_unified"
    default_scope = rows[0].get("scope") if rows else "connor"
    compressed_daily, important_memories = [], []
    retain_source_detail = bool(tier.get("retain_source_detail", False))
    covered = set(_valid_source_ids(parsed.get("discard_memory_ids"), by_id, limit=len(rows)))
    for item in parsed.get("compressed_daily") or []:
        normalized = _normalize_daily_draft_item(
            item, by_id=by_id, memory_kind="daily", source_limit=30, default_importance=0.25,
            compression_stage=tier["output_stage"], retain_source_detail=retain_source_detail,
        )
        if normalized:
            source_rows = [by_id[mem_id] for mem_id in normalized["source_memory_ids"]]
            normalized["room_id"], normalized["scope"] = _chatroom_target_for_rows(source_rows, default_room, default_scope)
            compressed_daily.append(normalized)
            covered.update(normalized["source_memory_ids"])
    for item in parsed.get("important_memories") or []:
        normalized = _normalize_daily_draft_item(
            item, by_id=by_id, memory_kind="long_term", source_limit=10, default_importance=0.0,
            retain_source_detail=retain_source_detail,
        )
        if normalized:
            source_rows = [by_id[mem_id] for mem_id in normalized["source_memory_ids"]]
            normalized["room_id"], normalized["scope"] = _chatroom_target_for_rows(source_rows, default_room, default_scope)
            important_memories.append(normalized)
            covered.update(normalized["source_memory_ids"])

    return {
        "ok": True,
        "error": "",
        "input_count": len(rows),
        "old_rows": [_review_old_row(row, "chatroom") for row in rows],
        "compressed_daily": compressed_daily,
        "important_memories": important_memories[:int(tier.get("max_important") or 2)],
        "discard_memory_ids": sorted(_valid_source_ids(parsed.get("discard_memory_ids"), by_id, limit=len(rows))),
        "covered_ids": sorted(covered),
        "remaining": len(rows) - len(covered),
        "message": str(parsed.get("message") or "").strip(),
        "raw_response": raw,
        "compression_tier": tier["key"],
        "tier_label": tier["label"],
        "output_stage": tier["output_stage"],
        "retain_source_detail": retain_source_detail,
    }


def _normalize_daily_compression_target(target: str | None) -> str:
    value = str(target or "main").strip().lower()
    return value if value in {"main", "chatroom", "both"} else "main"


def _empty_draft_payload(days: int, cutoff_ts: float, target: str) -> dict:
    return {
        "days": days,
        "cutoff_ts": cutoff_ts,
        "target": target,
        "main": {"batches": []},
        "chatroom": {"batches": []},
    }


def _daily_compression_counts(payload: dict) -> dict:
    def actor_counts(key: str) -> dict:
        batches = (payload.get(key) or {}).get("batches") or []
        covered = set()
        all_old_rows = []
        for batch in batches:
            covered.update(_batch_covered_ids(batch))
            all_old_rows.extend(batch.get("old_rows") or [])
        old_rows = [row for row in all_old_rows if row.get("id") in covered]
        return {
            "batches": len(batches),
            "input_count": sum(int(batch.get("input_count", 0)) for batch in batches),
            "processed": len(covered),
            "created_daily": sum(len(batch.get("compressed_daily") or []) for batch in batches),
            "created_important": sum(len(batch.get("important_memories") or []) for batch in batches),
            "remaining": sum(int(batch.get("remaining", 0)) for batch in batches),
            "messages": [batch.get("message", "") for batch in batches if batch.get("message")],
            "errors": [batch.get("error", "") for batch in batches if batch.get("error")],
            "old_rows": old_rows,
        }

    main = actor_counts("main")
    chatroom = actor_counts("chatroom")
    total = {
        "input_count": main["input_count"] + chatroom["input_count"],
        "processed": main["processed"] + chatroom["processed"],
        "created_daily": main["created_daily"] + chatroom["created_daily"],
        "created_important": main["created_important"] + chatroom["created_important"],
        "remaining": main["remaining"] + chatroom["remaining"],
        "errors": main["errors"] + chatroom["errors"],
    }
    return {"main": main, "chatroom": chatroom, "total": total}


def _batch_covered_ids(batch: dict) -> set[str]:
    covered = set(str(x).strip() for x in _json_list(batch.get("discard_memory_ids")) if str(x).strip())
    covered.update(str(x).strip() for x in _json_list(batch.get("covered_ids")) if str(x).strip())
    for field in ("compressed_daily", "important_memories"):
        for item in batch.get(field) or []:
            covered.update(str(x).strip() for x in _json_list(item.get("source_memory_ids")) if str(x).strip())
    old_ids = {str(row.get("id") or "").strip() for row in batch.get("old_rows") or []}
    return {mem_id for mem_id in covered if mem_id and (not old_ids or mem_id in old_ids)}


def _refresh_payload_covered_ids(payload: dict) -> dict:
    for key in ("main", "chatroom"):
        for batch in (payload.get(key) or {}).get("batches") or []:
            batch["covered_ids"] = sorted(_batch_covered_ids(batch))
            batch["remaining"] = max(0, int(batch.get("input_count", 0)) - len(batch["covered_ids"]))
    return payload


def _serialize_daily_compression_review(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    try:
        payload = json.loads(data.get("payload") or "{}")
    except Exception:
        payload = {}
    try:
        apply_result = json.loads(data.get("apply_result") or "{}")
    except Exception:
        apply_result = {}
    return {
        "id": data.get("id"),
        "target": data.get("target") or payload.get("target") or "both",
        "status": data.get("status"),
        "days": data.get("days"),
        "cutoff_ts": data.get("cutoff_ts"),
        "candidate_count": data.get("candidate_count"),
        "error": data.get("error") or "",
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "applied_at": data.get("applied_at"),
        "discarded_at": data.get("discarded_at"),
        "payload": payload,
        "counts": _daily_compression_counts(payload),
        "apply_result": apply_result,
    }


async def get_latest_daily_compression_review(target: str = "main") -> dict | None:
    await _ensure_daily_compression_schema()
    target = _normalize_daily_compression_target(target)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM daily_memory_compress_reviews "
            "WHERE status IN ('draft','failed') AND target=? "
            "ORDER BY created_at DESC LIMIT 1",
            (target,),
        )
        row = await cur.fetchone()
    return _serialize_daily_compression_review(row)


async def generate_daily_compression_draft(days: int = 15, target: str = "main") -> dict:
    await _ensure_daily_compression_schema()
    days = max(1, int(days or 15))
    target = _normalize_daily_compression_target(target)
    now_ts = time.time()
    cutoff_ts = now_ts - days * 86400
    model_key, _ = await _get_active_model_and_conv()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        main_rows = []
        chatroom_rows = []
        if target in {"main", "both"}:
            daily_types = tuple(SUMMARY_MEMORY_TYPES - {"seeky_compressed"})
            placeholders = ",".join("?" for _ in daily_types)
            cur = await db.execute(
                "SELECT id, content, type, created_at, source_conv, keywords, importance, "
                "source_start_ts, source_end_ts, source_msg_id, compression_stage "
                f"FROM memories WHERE LOWER(type) IN ({placeholders}) "
                "AND COALESCE(compression_stage,0) < ? "
                "AND COALESCE(source_end_ts, source_start_ts, created_at) < ? "
                "ORDER BY COALESCE(source_start_ts, created_at) ASC",
                (*daily_types, DAILY_COMPRESSION_FINAL_STAGE, cutoff_ts),
            )
            main_rows = [dict(row) for row in await cur.fetchall()]
        if target in {"chatroom", "both"}:
            cur = await db.execute(
                "SELECT id, room_id, scope, content, keywords, importance, created_at, "
                "source_start_ts, source_end_ts, source_msg_id, memory_kind, compression_stage "
                "FROM chatroom_memories "
                "WHERE memory_kind='daily' AND COALESCE(compression_stage,0) < ? "
                "AND COALESCE(source_end_ts, source_start_ts, created_at) < ? "
                "ORDER BY COALESCE(source_start_ts, created_at) ASC",
                (DAILY_COMPRESSION_FINAL_STAGE, cutoff_ts),
            )
            chatroom_rows = [dict(row) for row in await cur.fetchall()]

    main_by_tier = _daily_tier_rows(main_rows, now_ts)
    chatroom_by_tier = _daily_tier_rows(chatroom_rows, now_ts)
    main_rows = [row for rows in main_by_tier.values() for row in rows]
    chatroom_rows = [row for rows in chatroom_by_tier.values() for row in rows]
    candidate_count = len(main_rows) + len(chatroom_rows)
    if candidate_count <= 0:
        return {
            "ok": True,
            "review": None,
            "candidate_count": 0,
            "message": f"没有超过 {days} 天、尚未压缩的日常记忆。",
        }

    payload = _empty_draft_payload(days, cutoff_ts, target)
    payload["policy"] = "progressive"
    payload["keep_source_days"] = DAILY_COMPRESSION_KEEP_SOURCE_DAYS
    payload["tiers"] = [
        {k: v for k, v in tier.items() if k != "source_stages"} | {"source_stages": sorted(tier["source_stages"])}
        for tier in DAILY_COMPRESSION_TIERS
    ]
    for tier in DAILY_COMPRESSION_TIERS:
        for batch in _batch_daily_rows(main_by_tier.get(tier["key"], [])):
            payload["main"]["batches"].append(await _draft_main_daily_rows(batch, model_key, tier))
    chatroom_model = ""
    if chatroom_rows:
        from chatroom import load_chatroom_config
        chatroom_model = load_chatroom_config().get("connor_model") or "Codex"
        for tier in DAILY_COMPRESSION_TIERS:
            for batch in _batch_daily_rows(chatroom_by_tier.get(tier["key"], [])):
                payload["chatroom"]["batches"].append(await _draft_chatroom_daily_rows(batch, chatroom_model, tier))

    payload = _refresh_payload_covered_ids(payload)
    counts = _daily_compression_counts(payload)
    raw_response = "\n\n".join(
        batch.get("raw_response", "")
        for key in ("main", "chatroom")
        for batch in payload[key]["batches"]
        if batch.get("raw_response")
    )
    errors = counts["total"]["errors"]
    now = time.time()
    review_id = _daily_review_id()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO daily_memory_compress_reviews ("
            "id, target, status, days, cutoff_ts, model_main, model_chatroom, candidate_count, "
            "payload, raw_response, error, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id, target, "draft", days, cutoff_ts, model_key, chatroom_model, candidate_count,
                json.dumps(payload, ensure_ascii=False), raw_response,
                "；".join(errors), now, now,
            ),
        )
        await db.commit()

    review = await get_daily_compression_review(review_id)
    total = counts["total"]
    return {
        "ok": True,
        "review": review,
        "candidate_count": candidate_count,
        "message": (
            f"日常压缩草稿已生成：候选 {candidate_count} 条，拟压缩/丢弃 {total['processed']} 条，"
            f"新日常 {total['created_daily']} 条，新长期重要 {total['created_important']} 条。"
        ),
    }


async def get_daily_compression_review(review_id: str) -> dict | None:
    await _ensure_daily_compression_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM daily_memory_compress_reviews WHERE id=?", (review_id,))
        row = await cur.fetchone()
    return _serialize_daily_compression_review(row)


async def update_daily_compression_review(review_id: str, payload: dict) -> dict:
    await _ensure_daily_compression_schema()
    review = await get_daily_compression_review(review_id)
    if not review:
        return {"ok": False, "message": "Compression draft not found."}
    if review["status"] != "draft":
        return {"ok": False, "message": "Only draft compression reviews can be edited.", "review": review}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Invalid draft payload.", "review": review}
    payload.setdefault("target", review.get("target") or "main")
    payload.setdefault("days", review.get("days") or 15)
    payload = _refresh_payload_covered_ids(payload)
    candidate_count = _daily_compression_counts(payload)["total"]["input_count"]
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "UPDATE daily_memory_compress_reviews "
            "SET payload=?, candidate_count=?, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), candidate_count, now, review_id),
        )
        await db.commit()
    updated = await get_daily_compression_review(review_id)
    return {"ok": True, "review": updated, "message": "Compression draft saved."}


def _source_rows_from_draft_item(item: dict) -> list[dict]:
    fallback_ts = float(item.get("memory_time") or time.time())
    return [{
        "created_at": fallback_ts,
        "source_start_ts": item.get("source_start_ts") or fallback_ts,
        "source_end_ts": item.get("source_end_ts") or fallback_ts,
    }]


async def _delete_main_daily_ids(ids: set[str]) -> int:
    if not ids:
        return 0
    daily_types = tuple(SUMMARY_MEMORY_TYPES - {"seeky_compressed"})
    id_placeholders = ",".join("?" for _ in ids)
    type_placeholders = ",".join("?" for _ in daily_types)
    async with get_db() as db:
        cur = await db.execute(
            f"DELETE FROM memories WHERE id IN ({id_placeholders}) "
            f"AND LOWER(type) IN ({type_placeholders}) AND COALESCE(compression_stage,0)<?",
            (*sorted(ids), *daily_types, DAILY_COMPRESSION_FINAL_STAGE),
        )
        await db.commit()
        return cur.rowcount if cur.rowcount is not None else 0


async def _delete_chatroom_daily_ids(ids: set[str]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        cur = await db.execute(
            f"DELETE FROM chatroom_memories WHERE id IN ({placeholders}) "
            "AND memory_kind='daily' AND COALESCE(compression_stage,0)<?",
            (*sorted(ids), DAILY_COMPRESSION_FINAL_STAGE),
        )
        await db.commit()
        return cur.rowcount if cur.rowcount is not None else 0


async def _apply_main_daily_draft(payload: dict) -> dict:
    created_daily, created_important, covered = [], [], set()
    payload = _refresh_payload_covered_ids(payload)
    for batch in (payload.get("main") or {}).get("batches") or []:
        covered.update(_batch_covered_ids(batch))
        for item in batch.get("compressed_daily") or []:
            mem_id = await _insert_main_compressed_memory(
                content=str(item.get("content") or "").strip(),
                memory_type="daily",
                keywords=_normalize_daily_keywords(item.get("keywords")),
                importance=max(0.0, min(0.6, float(item.get("importance", 0.25)))),
                source_rows=_source_rows_from_draft_item(item),
                memory_time=float(item.get("memory_time") or time.time()),
                compression_stage=max(1, min(DAILY_COMPRESSION_FINAL_STAGE, int(item.get("compression_stage") or batch.get("output_stage") or 1))),
                source_msg_ids=_draft_item_source_msg_ids(item),
                evidence_summary=_draft_item_evidence_summary(item),
            )
            if mem_id:
                created_daily.append(mem_id)
        for item in batch.get("important_memories") or []:
            importance = float(item.get("importance", 0.0))
            if importance < 0.8:
                continue
            mem_id = await _insert_main_compressed_memory(
                content=str(item.get("content") or "").strip(),
                memory_type=LONG_TERM_MEMORY_TYPE,
                keywords=_normalize_daily_keywords(item.get("keywords")),
                importance=min(1.0, importance),
                source_rows=_source_rows_from_draft_item(item),
                memory_time=float(item.get("memory_time") or time.time()),
                compression_stage=0,
                source_msg_ids=_draft_item_source_msg_ids(item),
                evidence_summary=_draft_item_evidence_summary(item),
            )
            if mem_id:
                created_important.append(mem_id)
    deleted = await _delete_main_daily_ids(covered)
    if covered or created_daily or created_important:
        messages = [
            batch.get("message", "")
            for batch in (payload.get("main") or {}).get("batches") or []
            if batch.get("message")
        ]
        async with get_db() as db:
            await db.execute(
                "INSERT INTO daily_memory_compress_log (id, actor, old_ids, new_ids, important_ids, message, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f"dmc_{time.time_ns()}", "aion", json.dumps(sorted(covered), ensure_ascii=False),
                    json.dumps(created_daily, ensure_ascii=False), json.dumps(created_important, ensure_ascii=False),
                    "\n".join(messages), time.time(),
                ),
            )
            await db.commit()
    return {
        "deleted": deleted,
        "covered": len(covered),
        "created_daily": len(created_daily),
        "created_important": len(created_important),
        "new_ids": created_daily,
        "important_ids": created_important,
    }


async def _apply_chatroom_daily_draft(payload: dict) -> dict:
    from chatroom import save_chatroom_memory
    created_daily, created_important, covered = [], [], set()
    payload = _refresh_payload_covered_ids(payload)
    for batch in (payload.get("chatroom") or {}).get("batches") or []:
        covered.update(_batch_covered_ids(batch))
        for item in batch.get("compressed_daily") or []:
            mem_id = await save_chatroom_memory(
                room_id=item.get("room_id") or "connor_unified",
                scope=item.get("scope") or "connor",
                content=str(item.get("content") or "").strip(),
                keywords=",".join(_normalize_daily_keywords(item.get("keywords"))),
                importance=max(0.0, min(0.6, float(item.get("importance", 0.25)))),
                source_start_ts=item.get("source_start_ts"),
                source_end_ts=item.get("source_end_ts"),
                source_msg_id=json.dumps(_draft_item_source_msg_ids(item), ensure_ascii=False),
                memory_kind="daily",
                compression_stage=max(1, min(DAILY_COMPRESSION_FINAL_STAGE, int(item.get("compression_stage") or batch.get("output_stage") or 1))),
                evidence_summary=_draft_item_evidence_summary(item),
                evidence_detail_level="summary",
                created_at=float(item.get("memory_time") or item.get("source_start_ts") or time.time()),
            )
            if mem_id:
                created_daily.append(mem_id)
                await asyncio.sleep(0.001)
        for item in batch.get("important_memories") or []:
            importance = float(item.get("importance", 0.0))
            if importance < 0.8:
                continue
            mem_id = await save_chatroom_memory(
                room_id=item.get("room_id") or "connor_unified",
                scope=item.get("scope") or "connor",
                content=str(item.get("content") or "").strip(),
                keywords=",".join(_normalize_daily_keywords(item.get("keywords"))),
                importance=min(1.0, importance),
                source_start_ts=item.get("source_start_ts"),
                source_end_ts=item.get("source_end_ts"),
                source_msg_id=json.dumps(_draft_item_source_msg_ids(item), ensure_ascii=False),
                memory_kind="long_term",
                compression_stage=0,
                evidence_summary=_draft_item_evidence_summary(item),
                evidence_detail_level="summary",
                created_at=float(item.get("memory_time") or item.get("source_start_ts") or time.time()),
            )
            if mem_id:
                created_important.append(mem_id)
                await asyncio.sleep(0.001)
    deleted = await _delete_chatroom_daily_ids(covered)
    if covered or created_daily or created_important:
        messages = [
            batch.get("message", "")
            for batch in (payload.get("chatroom") or {}).get("batches") or []
            if batch.get("message")
        ]
        async with get_db() as db:
            await db.execute(
                "INSERT INTO daily_memory_compress_log (id, actor, old_ids, new_ids, important_ids, message, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f"dmc_{time.time_ns()}", "connor", json.dumps(sorted(covered), ensure_ascii=False),
                    json.dumps(created_daily, ensure_ascii=False), json.dumps(created_important, ensure_ascii=False),
                    "\n".join(messages), time.time(),
                ),
            )
            await db.commit()
    return {
        "deleted": deleted,
        "covered": len(covered),
        "created_daily": len(created_daily),
        "created_important": len(created_important),
        "new_ids": created_daily,
        "important_ids": created_important,
    }


async def _claim_daily_compression_review(review_id: str) -> tuple[bool, dict | None]:
    """Atomically reserve a draft so concurrent apply requests cannot both write it."""
    await _ensure_daily_compression_schema()
    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE daily_memory_compress_reviews "
            "SET status='applying', updated_at=? WHERE id=? AND status='draft'",
            (now, review_id),
        )
        claimed = cur.rowcount == 1
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM daily_memory_compress_reviews WHERE id=?",
            (review_id,),
        )
        row = await cur.fetchone()
    return claimed, _serialize_daily_compression_review(row)


async def apply_daily_compression_review(review_id: str) -> dict:
    await _ensure_daily_compression_schema()
    claimed, review = await _claim_daily_compression_review(review_id)
    if not review:
        return {"ok": False, "message": "没有找到这份压缩草稿。"}
    if not claimed:
        if review["status"] == "applied":
            return {
                "ok": True,
                "already_applied": True,
                "message": "这份压缩草稿已经应用过，没有重复写入。",
                "review": review,
            }
        if review["status"] == "applying":
            return {
                "ok": False,
                "message": "这份压缩草稿正在应用中，请勿重复操作。",
                "review": review,
            }
        return {"ok": False, "message": "这份压缩草稿当前不能应用。", "review": review}
    payload = _refresh_payload_covered_ids(review.get("payload") or {})
    try:
        main_result = await _apply_main_daily_draft(payload)
        chatroom_result = await _apply_chatroom_daily_draft(payload)
    except Exception as exc:
        now = time.time()
        async with get_db() as db:
            await db.execute(
                "UPDATE daily_memory_compress_reviews "
                "SET status='failed', error=?, updated_at=? WHERE id=? AND status='applying'",
                (str(exc), now, review_id),
            )
            await db.commit()
        failed = await get_daily_compression_review(review_id)
        return {"ok": False, "message": f"应用压缩草稿失败：{exc}", "review": failed}
    apply_result = {"main": main_result, "chatroom": chatroom_result}
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "UPDATE daily_memory_compress_reviews "
            "SET status='applied', apply_result=?, error='', applied_at=?, updated_at=? "
            "WHERE id=? AND status='applying'",
            (json.dumps(apply_result, ensure_ascii=False), now, now, review_id),
        )
        await db.commit()
    applied = await get_daily_compression_review(review_id)
    total_new_daily = main_result["created_daily"] + chatroom_result["created_daily"]
    total_new_important = main_result["created_important"] + chatroom_result["created_important"]
    total_deleted = main_result["deleted"] + chatroom_result["deleted"]
    return {
        "ok": True,
        "review": applied,
        "message": f"压缩草稿已应用：删除旧日常 {total_deleted} 条，新日常 {total_new_daily} 条，新长期重要 {total_new_important} 条。",
    }


async def discard_daily_compression_review(review_id: str) -> dict:
    await _ensure_daily_compression_schema()
    review = await get_daily_compression_review(review_id)
    if not review:
        return {"ok": False, "message": "没有找到这份压缩草稿。"}
    if review["status"] == "applied":
        return {"ok": False, "message": "已应用的草稿不能废弃。", "review": review}
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "UPDATE daily_memory_compress_reviews "
            "SET status='discarded', discarded_at=?, updated_at=? WHERE id=?",
            (now, now, review_id),
        )
        await db.commit()
    discarded = await get_daily_compression_review(review_id)
    return {"ok": True, "review": discarded, "message": "压缩草稿已废弃。"}


async def compress_expired_daily_memories(days: int = 15) -> dict:
    """Compatibility wrapper: create a draft instead of applying immediately."""
    return await generate_daily_compression_draft(days=days, target="both")


async def rebuild_embeddings() -> dict:
    """重建向量索引：用当前配置的 embedding 模型为所有记忆重新生成向量，不触发 AI 总结"""
    success = 0
    failed = 0
    total = 0
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # 主聊天记忆表
        cur = await db.execute("SELECT id, content FROM memories ORDER BY id")
        rows = await cur.fetchall()
        total += len(rows)
        for row in rows:
            emb = await get_embedding(row["content"][:2000])
            if emb:
                await db.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (_pack_embedding(emb), row["id"])
                )
                success += 1
            else:
                failed += 1
            if success % 5 == 0:
                await db.commit()
                await asyncio.sleep(0.3)
        await db.commit()
        # 聊天室记忆表
        try:
            cur2 = await db.execute("SELECT id, content FROM chatroom_memories ORDER BY id")
            cr_rows = await cur2.fetchall()
            total += len(cr_rows)
            for row in cr_rows:
                emb = await get_embedding(row["content"][:2000])
                if emb:
                    await db.execute(
                        "UPDATE chatroom_memories SET embedding = ? WHERE id = ?",
                        (_pack_embedding(emb), row["id"])
                    )
                    success += 1
                else:
                    failed += 1
                if success % 5 == 0:
                    await db.commit()
                    await asyncio.sleep(0.3)
            await db.commit()
        except Exception:
            pass  # 聊天室记忆表可能不存在
    print(f"[Memory] 向量索引重建完成: {success}/{total} 成功, {failed} 失败")
    return {"total": total, "success": success, "failed": failed}
