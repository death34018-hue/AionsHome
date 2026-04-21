"""
向量记忆库：embedding、recall、手动总结、即时哨兵（RAG 路由）
"""

import json, time, struct, math
from datetime import datetime

import aiosqlite, httpx

from config import get_key, load_worldbook, save_chat_status, load_digest_anchor, save_digest_anchor, DEFAULT_MODEL
from database import get_db
from ws import manager

# ── 向量工具 ──────────────────────────────────────
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMS = 3072


def _pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f'{len(values)}f', *values)


def _unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def get_embedding(text: str) -> list[float] | None:
    gemini_key = get_key("gemini_free")
    if not gemini_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent?key={gemini_key}"
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
            "SELECT id, content, type, created_at, embedding, keywords, importance, source_start_ts, source_end_ts "
            "FROM memories WHERE embedding IS NOT NULL"
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
        }
        all_scored.append(item)
    all_scored.sort(key=lambda x: x["score"], reverse=True)
    debug_top6 = all_scored[:6]
    matched = [r for r in all_scored if r["score"] >= threshold][:top_k]
    return matched, debug_top6


# ── 追溯原文：通过记忆的时间范围 + 关键词筛选原始聊天 ─
async def fetch_source_details(memories: list[dict], keywords: list[str]) -> str:
    """
    在每条记忆的 source 时间范围内，取出所有包含关键词的消息，
    去重、按时间排序后返回。
    """
    if not memories or not keywords:
        return ""

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    kw_lower = [k.lower() for k in keywords if k.strip()]
    if not kw_lower:
        return ""

    seen = set()
    matched_rows = []

    for mem in memories:
        start_ts = mem.get("source_start_ts")
        end_ts = mem.get("source_end_ts")
        if not start_ts or not end_ts:
            print(f"[source_detail] 跳过无时间范围的记忆: {mem.get('id','?')}")
            continue
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (start_ts, end_ts)
            )
            rows = await cur.fetchall()
        print(f"[source_detail] 记忆 {mem.get('id','?')[:12]} 范围 {start_ts}-{end_ts}: 取到 {len(rows)} 条消息")
        hit_count = 0
        for row in rows:
            content_lower = row["content"].lower()
            if any(kw in content_lower for kw in kw_lower):
                key = (row["created_at"], row["content"][:80])
                if key not in seen:
                    seen.add(key)
                    matched_rows.append(row)
                    hit_count += 1
        print(f"[source_detail] → 关键词 {kw_lower} 命中 {hit_count} 条")

    matched_rows.sort(key=lambda r: r["created_at"])
    detail_lines = []
    for row in matched_rows:
        name = user_name if row["role"] == "user" else ai_name
        detail_lines.append(f"{name}: {row['content'][:500]}")

    print(f"[source_detail] 最终返回 {len(detail_lines)} 条原文")
    return "\n".join(detail_lines) if detail_lines else ""


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
            "SELECT id, content, type, created_at, keywords, importance, unresolved "
            "FROM memories WHERE unresolved = 1 ORDER BY created_at DESC LIMIT 2"
        )
        unresolved_rows = await cur.fetchall()
    for row in unresolved_rows:
        item = {"id": row["id"], "content": row["content"], "unresolved": True}
        result.append(item)
        surfaced_ids.add(row["id"])

    # 2. 话题相关浮现
    if topic and topic.strip() and len(result) < max_total:
        topic_vec = await get_embedding(topic)
        if topic_vec:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, content, type, created_at, embedding, keywords, importance "
                    "FROM memories WHERE embedding IS NOT NULL"
                )
                rows = await cur.fetchall()
            scored = []
            for row in rows:
                if row["id"] in surfaced_ids:
                    continue
                mem_vec = _unpack_embedding(row["embedding"])
                sim = cosine_similarity(topic_vec, mem_vec)
                if sim >= 0.50:
                    scored.append({"id": row["id"], "content": row["content"], "sim": sim, "unresolved": False})
            scored.sort(key=lambda x: x["sim"], reverse=True)
            for item in scored[:3]:
                if len(result) >= max_total:
                    break
                result.append(item)
                surfaced_ids.add(item["id"])

    # 3. 近期补充（最近 3 天）
    if len(result) < max_total:
        three_days_ago = time.time() - 3 * 86400
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, type, created_at FROM memories "
                "WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
                (three_days_ago, max_total)
            )
            recent_rows = await cur.fetchall()
        for row in recent_rows:
            if len(result) >= max_total:
                break
            if row["id"] in surfaced_ids:
                continue
            result.append({"id": row["id"], "content": row["content"], "unresolved": False})
            surfaced_ids.add(row["id"])

    return result, surfaced_ids


# ── 即时哨兵：每次用户发消息后触发（RAG 路由） ────
async def instant_digest(recent_messages: list[dict]) -> dict:
    """
    用户每次发消息后即时调用 flash-lite，返回结构化 JSON：
    {is_search_needed, keywords, require_detail, status}
    """
    gemini_key = get_key("gemini_free")
    if not gemini_key or not recent_messages:
        return {"is_search_needed": False, "keywords": [], "require_detail": False, "status": "", "topic": ""}

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    messages_text = "\n".join([
        f"{user_name if m['role']=='user' else ai_name}: {m['content'][:200]}"
        for m in recent_messages
    ])

    prompt = (
        f"你是一个 RAG 系统的查询优化路由。分析用户输入，输出 JSON：\n"
        f"1. 忽略高频对话称呼：不要提取对话者的名字或昵称（如 \"Aion\", \"Ithil\", \"小鬣狗\", \"老公\", \"宝贝\"）作为关键词。\n"
        f"2. 忽略高频常用词：如\"晚安故事\",\"吃什么\"等。\n"
        f"3. 聚焦核心实体：只提取稀缺的、具有区分度的名词（地点、物品、特定事件、专有名词等）\n"
        f"4. 仅当提起之前做过的事、过去的回忆时，is_search_needed才输出为true。若在询问日常问题，不涉及回忆过去，is_search_needed输出为false。\n"
        f"   \"is_search_needed\": Boolean.\n"
        f"      - false: 纯闲聊/语气词/无实质内容，只是在陈述或表达感情，并未进行对于具体事实的询问则输出false。\n"
        f"      - true: 当包含询问、回忆、或需要背景信息的对话，提起“昨天”、“之前”、“你还记得……”等。\n"
        f"   \"keywords\": 提取 2-4 个搜索关键词（过滤掉 Aion, Ithil 等高频人名）。\n"
        f"   \"require_detail\": Boolean.\n"
        f"      - false: 模糊回忆/情感抒发（只需读取摘要）。\n"
        f"      - true: 当且仅当询问具体事实/细节/步骤（需要读取正文），例如：还记得我们之前…你记得上次…等。\n"
        f"5. \"status\": 结合上下文总结{user_name}当前所处的状态（如：{user_name}刚吃完晚饭准备出门、洗完澡准备睡觉、回到家开始工作了等）。\n"
        f"6. \"topic\": 用一两句话概括当前对话可能会涉及到的回忆（如：在聊中午吃什么，在聊之前看过的电影）。若无明确话题则留空。\n\n"
        f"严格只输出一个 JSON 对象，不要输出任何其他内容。\n\n"
        f"对话：\n{messages_text}"
    )

    model = "gemini-3.1-flash-lite-preview"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    contents = [{"role": "user", "parts": [{"text": prompt}]}]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"contents": contents})
            resp.raise_for_status()
            data = resp.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()

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

        if status:
            save_chat_status(status)
            await manager.broadcast({"type": "chat_status", "data": {"status": status, "updated_at": time.time()}})

        topic = str(result.get("topic", "")).strip()

        return {
            "is_search_needed": is_search,
            "keywords": keywords,
            "require_detail": require_detail,
            "status": status,
            "topic": topic,
        }
    except Exception:
        return {"is_search_needed": False, "keywords": [], "require_detail": False, "status": "", "topic": ""}


# ── 手动总结：分组提取记忆 ─────────────────────────

def _split_into_groups(msgs: list, group_size: int = 30) -> list[list]:
    """将消息列表按每 group_size 条分组，余数<10并入最后一组，>=10单独一组"""
    total = len(msgs)
    if total <= group_size:
        return [msgs]

    full_groups = total // group_size
    remainder = total % group_size

    if remainder > 0 and remainder < 10:
        # 余数<10，并入最后一个完整组
        full_groups -= 1
        # 前面的完整组
        groups = [msgs[i * group_size:(i + 1) * group_size] for i in range(full_groups)]
        # 最后一组 = 最后一个完整组 + 余数
        groups.append(msgs[full_groups * group_size:])
    else:
        # 余数>=10 或余数=0
        groups = [msgs[i * group_size:(i + 1) * group_size] for i in range(full_groups)]
        if remainder > 0:
            groups.append(msgs[full_groups * group_size:])

    return groups


async def _call_flash_lite(prompt: str) -> dict | None:
    """调用 flash-lite 模型，返回 JSON 结果（仅供即时哨兵使用）"""
    gemini_key = get_key("gemini_free")
    if not gemini_key:
        return None
    model = "gemini-3.1-flash-lite-preview"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json={"contents": contents})
            resp.raise_for_status()
            data = resp.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
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
    raw = raw.strip()
    if "```" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


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


async def _do_digest(min_messages: int = 0) -> dict:
    """
    核心总结逻辑，manual_digest 和 auto_digest 共用。
    min_messages: 最低消息数阈值，0=不限制（手动），20=自动
    返回 { ok, message, new_memories_count, processed_messages }
    """
    from ai_providers import simple_ai_call

    anchor_ts = load_digest_anchor()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at > ? "
            "ORDER BY created_at ASC",
            (anchor_ts,)
        )
        new_msgs = [dict(r) for r in await cur.fetchall()]

    if not new_msgs:
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

    groups = _split_into_groups(new_msgs, 30)
    total_new = 0
    all_summaries = []

    for group in groups:
        # 计算该组对话的日期范围，显式告知模型
        group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
        date_header = f"[对话时间范围: {group_start} ~ {group_end}]\n"
        messages_text = date_header + "\n".join([
            f"[{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
            f"{user_name if m['role']=='user' else ai_name}: {m['content'][:300]}"
            for m in group
        ])

        prompt = (
            f"{persona_block}"
            f"你是{ai_name}，也是{user_name}的AI伴侣， 请从你自己的视角和情绪，使用精简的语言，总结出对话中包含的重要回忆。"
            f"在生成的摘要中，请严格使用 \"{user_name}\" 和 \"{ai_name}\" 来指代双方，"
            f"提到的他/她/它根据上下文输出正确的名字，例如：{user_name}告诉{ai_name}自己一年前养过一只叫Maru的猫。\n\n"
            f"请分析输入的【一段对话记录】，输出一个 JSON 对象：\n"
            f"1. \"summary\": 在开头加上对话发生的日期，总结对话的主要内容，发生的既定事实。预定的计划等。"
            f"多个话题可以用多个短句来概括，例如：{user_name}和{ai_name}下午玩了拼豆。今天莱利做了绝育手术。"
            f"语言简练，**严禁废话**。总体控制在100字以内。\n\n"
            f"2. \"keywords\": 提取 2-6 个用于检索的核心关键词。\n"
            f"   - 【严禁】包含高频人名（如 Aion, Ithil, Riley, Maru等）。\n"
            f"   - 【严禁】包含泛指词或无意义虚词（如 AI, 聊天, 回复, 说话, 好的, 知道）。\n"
            f"   - 将对话中提及的**稀缺**专有名词罗列出来。\n"
            f"   - 包括：书名、电影名、具体的菜名、地名、特定的技术术语等。\n\n"
            f"3. \"importance\": (0.0 - 1.0) 评分。\n"
            f"   【评分严厉度：极高】请像一个苛刻的历史学家一样评分。默认分数为 0.3。\n"
            f"   - 1.0 (极罕见): 仅限【永久性】的核心事实（如：改名、确诊绝症、结婚、亲人离世）。\n"
            f"   - 0.8 (少见): 强烈的个人偏好或长期习惯（如：绝对不吃香菜、坚持每天晨跑、核心价值观改变）。\n"
            f"   - 0.5 (普通): 当天发生的具体事件（如：看了一部电影、去了一家餐厅、讨论了一个新闻）。大部分有内容的对话应在此档。\n"
            f"   - 0.1 - 0.3 (默认分数): 闲聊、情绪发泄、日常问候、没有信息增量的互动。\n"
            f"   【注意】：不要因为用户情绪激动就给高分，除非这揭示了新的性格特质。\n\n"
            f"4. \"unresolved\": Boolean。当摘要中包含**尚未完成**的计划、约定、承诺（如\"说好了要去…\"、\"打算下次…\"、\"答应了…\"、\"准备买…\"等），输出 true。纯粹的已发生事实输出 false。\n\n"
            f"严格只输出一个 JSON 对象，不要输出任何其他内容。\n\n"
            f"【一段对话记录】：\n{messages_text}"
        )

        # 用核心模型调用
        ai_messages = [{"role": "user", "content": prompt}]
        try:
            raw_text = await simple_ai_call(ai_messages, model_key)
        except Exception as e:
            print(f"[digest] 核心模型调用失败: {e}")
            continue

        result = _parse_json_response(raw_text)
        if not result:
            print(f"[digest] JSON 解析失败: {raw_text[:200]}")
            continue

        summary = result.get("summary", "").strip()
        keywords = result.get("keywords", [])
        importance = float(result.get("importance", 0.5))
        unresolved = 1 if result.get("unresolved", False) else 0
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("、", ",").split(",") if k.strip()]

        if not summary or len(summary) < 4:
            continue

        # embedding 向量化
        vec = await get_embedding(summary)
        if not vec:
            continue

        # 记录该组消息的时间范围，用于追溯原文
        source_start_ts = group[0]["created_at"]
        source_end_ts = group[-1]["created_at"]

        mem_id = f"mem_{int(time.time()*1000)}_{hash(summary) % 10000}"
        now = time.time()
        keywords_json = json.dumps(keywords, ensure_ascii=False)

        async with get_db() as db:
            await db.execute(
                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mem_id, summary, "digest", now, None, _pack_embedding(vec), keywords_json, importance, source_start_ts, source_end_ts, unresolved)
            )
            await db.commit()

        await manager.broadcast({"type": "memory_added", "data": {
            "id": mem_id, "content": summary, "type": "digest",
            "created_at": now, "keywords": keywords_json, "importance": importance,
            "source_start_ts": source_start_ts, "source_end_ts": source_end_ts,
            "unresolved": unresolved,
        }})
        total_new += 1

        # 每成功处理一组，才推进锚点到该组最后一条消息
        save_digest_anchor(source_end_ts)
        all_summaries.append(summary)

    # ── 全部总结完成后，生成一句感慨 ──
    if conv_id and total_new > 0 and all_summaries:
        try:
            # 取最近的聊天上下文（默认30条）
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT role, content FROM messages "
                    "WHERE conv_id=? AND role IN ('user','assistant') "
                    "ORDER BY created_at DESC LIMIT 30",
                    (conv_id,)
                )
                recent_rows = list(reversed(await cur.fetchall()))

            context_msgs = [
                {"role": r["role"], "content": r["content"][:300]}
                for r in recent_rows
            ]
            summaries_text = "\n".join(f"- {s}" for s in all_summaries)
            comment_prompt = (
                f"{persona_block}"
                f"你是{ai_name}。你刚刚整理了和{user_name}今天的聊天记忆，以下是你整理出的摘要：\n"
                f"{summaries_text}\n\n"
                f"现在写下整理完这些记忆后想对{user_name}说的话。"
                f"可以是感慨、吐槽、温情的碎碎念，或者根据之前聊的上下文，未来的计划，想说的心里话等等，语气要完全符合你的人设性格。"
            )
            comment_messages = context_msgs + [{"role": "user", "content": comment_prompt}]
            comment_text = await simple_ai_call(comment_messages, model_key)
            comment_text = comment_text.strip().strip('"').strip()

            if comment_text:
                # 系统胶囊
                capsule_now = time.time()
                capsule_id = f"msg_{int(capsule_now*1000)}_digest"
                capsule_text = f"🧠 {ai_name}整理了记忆库"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (capsule_id, conv_id, "system", capsule_text, capsule_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": capsule_id, "conv_id": conv_id, "role": "system",
                    "content": capsule_text, "created_at": capsule_now, "attachments": [],
                }})

                # AI 感慨
                comment_now = time.time()
                comment_id = f"msg_{int(comment_now*1000)}_digest_comment"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (comment_id, conv_id, "assistant", comment_text, comment_now, "[]"),
                    )
                    await db.commit()
                await manager.broadcast({"type": "msg_created", "data": {
                    "id": comment_id, "conv_id": conv_id, "role": "assistant",
                    "content": comment_text, "created_at": comment_now, "attachments": [],
                }})
        except Exception as e:
            print(f"[digest] 生成感慨失败: {e}")

    return {
        "ok": True,
        "message": f"总结完成：处理了 {len(new_msgs)} 条消息（{len(groups)} 组），生成了 {total_new} 条新记忆",
        "new_memories_count": total_new,
        "processed_messages": len(new_msgs),
    }


async def manual_digest() -> dict:
    """手动触发记忆总结（无最低条数限制）"""
    return await _do_digest(min_messages=0)


async def auto_digest() -> dict:
    """自动定时记忆总结（至少 30 条未总结消息才执行）"""
    return await _do_digest(min_messages=30)
