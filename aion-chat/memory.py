"""
向量记忆库：embedding、recall、手动总结、本地前置路由
"""

from reply_timing import timed
import json, time, struct, math, asyncio, re
from datetime import datetime, timedelta

import aiosqlite, httpx

from config import get_key, get_sentinel_config, get_embedding_config, load_worldbook, load_digest_anchor, save_digest_anchor, DEFAULT_MODEL
from database import get_db
from model_json import extract_json_object
from ws import manager
from system_logs import logged_ai_call

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


def memory_kind_for_type(memory_type: str) -> str:
    """Keep social memories separate from daily summaries and important memories."""
    if str(memory_type or '').strip().lower() == 'friend':
        return 'friend'
    return "daily" if str(memory_type or "").strip().lower() in SUMMARY_MEMORY_TYPES else "long_term"


def memory_kind_label(memory_type: str) -> str:
    return {'daily':'日常', 'long_term':'长期重要', 'friend':'朋友记忆'}[memory_kind_for_type(memory_type)]


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


_embedding_client: httpx.AsyncClient | None = None


def _get_embedding_client() -> httpx.AsyncClient:
    global _embedding_client
    if _embedding_client is None or _embedding_client.is_closed:
        # 在应用事件循环内按需创建；认证和模型仍逐次从配置读取。
        _embedding_client = httpx.AsyncClient(
            timeout=30, limits=httpx.Limits(keepalive_expiry=60),
        )
    return _embedding_client


async def close_embedding_client() -> None:
    global _embedding_client
    client, _embedding_client = _embedding_client, None
    if client is not None:
        await client.aclose()


@timed("embedding")
async def get_embedding(text: str, *, timeout: float | None = None) -> list[float] | None:
    """聊天召回可指定总等待上限；后台写入沿用客户端的 30 秒超时。"""
    ecfg = get_embedding_config()
    if not ecfg["api_key"]:
        return None
    if ecfg["use_openai"]:
        # OpenAI 兼容格式（硅基流动等）
        url = f"{ecfg['base_url']}/v1/embeddings"
        headers = {"Authorization": f"Bearer {ecfg['api_key']}", "Content-Type": "application/json"}
        body = {"model": ecfg["model"], "input": text}
        try:
            async with asyncio.timeout(timeout):
                resp = await _get_embedding_client().post(url, json=body, headers=headers)
            if resp.status_code != 200:
                print(f"[Embedding] OpenAI 兼容调用失败 {resp.status_code}: {resp.text[:300]}")
                return None
            return resp.json()["data"][0]["embedding"]
        except Exception as e:
            print(f"[Embedding] 调用异常: {type(e).__name__}: {e}")
            return None
    else:
        # Gemini 原生格式
        model = ecfg["model"]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={ecfg['api_key']}"
        body = {"content": {"parts": [{"text": text}]}}
        try:
            async with asyncio.timeout(timeout):
                resp = await _get_embedding_client().post(url, json=body)
            resp.raise_for_status()
            return resp.json()["embedding"]["values"]
        except Exception:
            return None


# ── 关键词匹配辅助 ──────────────────────
def _parse_memory_keywords(value) -> list[str]:
    """兼容新式 JSON 数组与旧式中英文逗号分隔关键词。"""
    if isinstance(value, list):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            parsed = decoded if isinstance(decoded, list) else re.split(r"[,，、;；\n]+", text)
        except (json.JSONDecodeError, TypeError):
            parsed = re.split(r"[,，、;；\n]+", text)
    return [str(item).strip() for item in parsed if str(item).strip()]


def _keyword_match_score(query_keywords: list[str], mem_keywords_json: str) -> float:
    """计算关键词命中率：命中关键词数 / 查询关键词数"""
    if not query_keywords:
        return 0.0
    mem_kws = _parse_memory_keywords(mem_keywords_json)
    if not mem_kws:
        return 0.0
    mem_kws_lower = [k.lower() for k in mem_kws]
    hits = sum(1 for qk in query_keywords if any(qk.lower() in mk or mk in qk.lower() for mk in mem_kws_lower))
    return hits / len(query_keywords)


# ── 记忆召回（向量 + 关键词 + 重要度 综合评分）────
@timed("main_recall")
async def recall_memories(query_text: str, query_keywords: list[str] = None,
                          top_k: int = 5, threshold: float = 0.45) -> tuple[list[dict], list[dict]]:
    """
    综合评分 = 向量相似度×0.6 + 关键词命中率×0.3 + 重要度×0.1
    threshold 为最终得分门槛。
    返回 (matched, debug_top6): matched 为达标结果, debug_top6 为得分最高的前6条（含未达标）
    """
    query_vec = await get_embedding(query_text, timeout=4)
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
@timed("memory_sources")
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
        if str(mem.get('id','')).startswith('extmem_'):
            evidence_blocks.append('[外出回忆来源]\n' + str(mem.get('evidence_summary') or '来源原文未保留'))
            continue
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
@timed("main_surfacing")
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
        topic_vec = await get_embedding(topic, timeout=4)
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


@logged_ai_call("sentinel_text")
async def _call_sentinel_text(scfg: dict, prompt: str, timeout: int = 60) -> str | None:
    """统一调用哨兵模型（纯文本），支持 Gemini 原生和 OpenAI 兼容格式"""
    if scfg.get("provider") == "codex":
        from ai_providers import call_codex_sentinel
        return await call_codex_sentinel(
            prompt,
            model=scfg.get("model") or "gpt-5.6-luna",
            timeout=timeout,
        )
    if scfg.get("use_openai"):
        url = f"{scfg['base_url']}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {scfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": scfg["model"],
            "messages": [{"role": "user", "content": prompt}],
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


@logged_ai_call("sentinel_vision")
async def _call_sentinel_vision(scfg: dict, prompt: str, img_b64: str, mime_type: str = "image/jpeg", timeout: int = 60) -> str | None:
    """统一调用哨兵模型（带图片），支持 Gemini 原生和 OpenAI 兼容格式"""
    if scfg.get("provider") == "codex":
        from ai_providers import call_codex_sentinel
        return await call_codex_sentinel(
            prompt,
            model=scfg.get("model") or "gpt-5.6-luna",
            image_b64=img_b64,
            mime_type=mime_type,
            timeout=timeout,
        )
    if scfg.get("use_openai"):
        url = f"{scfg['base_url']}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {scfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": scfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}}
            ]}],
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


# ── 本地前置路由：每次用户发消息后触发（不调用模型） ────
_MEMORY_REFERENCE_RE = re.compile(
    r"(昨天|前天|上次|之前|以前|刚才|那天|前几天|还记得|记不记得|"
    r"看过|听过|说过|聊过|讲过|做过|吃过|买过|去过)"
)
_DETAIL_REQUEST_RE = re.compile(
    r"(讲的啥|讲什么|说的啥|叫什么|叫啥|名字|细节|具体|哪|什么|怎么|为什么|大概|内容)"
)
_FRONT_ROUTE_NEGATIONS = (
    "不是", "不要", "别让", "不让", "别叫", "别问", "不用", "不必",
)
_FRONT_ROUTE_SPEAK_ACTION = r"(?:先|优先)(?:来)?(?:说|回答|回复|讲|答)"


def _latest_user_text(recent_messages: list[dict]) -> str:
    for msg in reversed(recent_messages or []):
        if msg.get("role") == "user":
            return str(msg.get("content") or "").strip()
    if recent_messages:
        return str(recent_messages[-1].get("content") or "").strip()
    return ""


def _local_front_topic(text: str) -> str:
    """Use the user's own words as the vector-recall query."""
    cleaned = re.sub(r"\[\[image:[^\]]+\]\]", "", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:200] or "当前对话"


def _front_route_is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 8):start]
    return any(marker in prefix for marker in _FRONT_ROUTE_NEGATIONS)


def _front_route_mentions(text: str, name: str) -> list[int]:
    if not name:
        return []
    return [
        match.start()
        for match in re.finditer(re.escape(name), text, flags=re.IGNORECASE)
        if not _front_route_is_negated(text, match.start())
    ]


def _front_route_explicit_orders(text: str, name: str) -> list[int]:
    if not name:
        return []
    escaped = re.escape(name)
    patterns = (
        rf"{escaped}(?:老公)?\s*{_FRONT_ROUTE_SPEAK_ACTION}",
        rf"(?:先|优先)\s*(?:让|请|叫)?\s*{escaped}(?:老公)?(?:来)?(?:说|回答|回复|讲|答)?",
    )
    starts = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            name_match = re.search(escaped, match.group(0), flags=re.IGNORECASE)
            name_start = match.start() + (name_match.start() if name_match else 0)
            if not _front_route_is_negated(text, name_start):
                starts.append(match.start())
    return starts


def _resolve_local_first_responder(
    latest_user: str,
    participant_names: dict[str, str] | None,
) -> str:
    """Resolve explicit group-chat addressing without waiting for an LLM."""
    if not participant_names:
        return "random"

    names = {
        "aion": str(participant_names.get("aion") or "").strip(),
        "connor": str(participant_names.get("connor") or "").strip(),
    }
    ordered = []
    for actor, name in names.items():
        ordered.extend(
            (start, actor)
            for start in _front_route_explicit_orders(latest_user, name)
        )
    if ordered:
        actors = {actor for _, actor in ordered}
        if len(actors) == 1:
            return next(iter(actors))
        return min(ordered)[1]

    mentioned = [
        actor
        for actor, name in names.items()
        if _front_route_mentions(latest_user, name)
    ]
    return mentioned[0] if len(mentioned) == 1 else "random"


@timed("local_routing")
async def instant_digest(
    recent_messages: list[dict],
    group_participants: dict[str, str] | None = None,
) -> dict:
    """Build the per-message recall and reply route locally, without an LLM call."""
    if not recent_messages:
        return {
            "is_search_needed": False, "keywords": [], "require_detail": False,
            "status": "", "topic": "", "first_responder": "random",
        }

    latest_user = _latest_user_text(recent_messages)
    return {
        "is_search_needed": bool(_MEMORY_REFERENCE_RE.search(latest_user)),
        "keywords": [],
        "require_detail": bool(_DETAIL_REQUEST_RE.search(latest_user)),
        "status": "",
        "topic": _local_front_topic(latest_user),
        "first_responder": _resolve_local_first_responder(
            latest_user,
            group_participants,
        ),
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
        f"人物称谓：content 统一使用第三人称姓名叙述，记忆所属角色写作{actor_name}，用户写作{user_name}，其他人物沿用原文姓名。"
        "不使用“我、我们、你、你们、他、她、他们、她们”等人称代词代替人物姓名；每条须独立写清人物，不依赖其他条目补足指代。"
        "原文引语若含人称代词，改为姓名明确的转述，不冒充逐字引用；仍可保留有依据的个人感受。\n"
        "8. 不要输出解释型来源说明，不要写“这说明了什么”。来源原文由后端按 source_message_ids 读取真实消息。\n"
        "9. source_message_ids 能引用真实支撑消息时就填 1-6 个；找不到或拿不准时可以留空数组，不要为了凑来源而编造 id。\n"
        "10. 每 50 条消息通常产出 1-3 条 daily。宁可少写，也不要把普通流水账塞进记忆库。\n\n"
        "11. unresolved 必须固定输出 false。不要自行标记未完成。\n\n"
        "type 规则：\n"
        "- daily：有明确日期和对象的普通事件、短期目标、项目进展、具体测试反馈、有趣小事、关系氛围、可帮助自然陪伴的生活线索。普通流水账不要写。\n"
        "- important：非常重要，值得长期记录的记忆。这条当严格使用，严禁滥用。只能记录发生过的事实，例如宠物死了，分手，以及恋人明确要求你记住的事情。否则不能随意输出。\n\n"
        f"keywords：提取 1-5 个稀缺关键词，必须包含这条记忆的 YYYY-MM-DD 日期关键词；过滤高频人名/称呼（如 {ignored_text}）和泛词（AI、聊天、回复、知道、好的）。\n"
        "importance：daily 通常 0.25-0.65；important 必须 >=0.75。这个打分是当前记忆值得记多久，永久记住则为1.分数越小尝试着这条记忆可随着时间逐渐淡忘。不要因为情绪强烈就给高分，除非它揭示稳定事实。\n\n"
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
    from profile_digest import prepare as prepare_profile_digest, store_result as store_profile_result

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

            # ── 所有群聊窗口中主记忆锚点之后的消息 ──
            cur = await db.execute(
                "SELECT m.id, m.sender, m.content, m.created_at FROM chatroom_messages m "
                "JOIN chatroom_rooms r ON r.id = m.room_id "
                "WHERE r.type = 'group' AND m.created_at > ? AND m.sender != 'system' "
                "ORDER BY m.created_at ASC",
                (anchor_ts,),
            )
            for r in await cur.fetchall():
                d = dict(r)
                # 映射 sender → role（主 AI 视角）
                d["role"] = "assistant" if d["sender"] == "aion" else "user"
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
            lines.append(f"[{ts}][id={source_id}]{tag} {name}: {m['content']}")
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
        try:
            profile_context = await prepare_profile_digest(group, ai_name, user_name)
            if profile_context:
                prompt += profile_context['prompt']
            ai_messages = [{"role": "user", "content": prompt}]
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

        try:
            await store_profile_result(profile_context, result)
        except Exception as e:
            print(f"[digest] 画像更新尚未持久化，保留本组锚点等待重试: {type(e).__name__}: {e}")
            digest_incomplete = True
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


# Seeky 导入仍共用时间解析；旧版草稿压缩流程已退役。
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
