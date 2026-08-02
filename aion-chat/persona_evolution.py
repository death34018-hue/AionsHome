import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from ai_providers import simple_ai_call
from config import DEFAULT_MODEL, load_worldbook, save_worldbook
from database import get_db


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
EVOLUTION_RUN_RETENTION_SECONDS = 72 * 60 * 60
ACTOR_MAIN_AI = "main_ai"
ACTOR_CONNOR = "connor"
EVOLUTION_ACTORS = {ACTOR_MAIN_AI, ACTOR_CONNOR}

_AI_SECTION_LABELS = [
    ("identity_core", "核心身份"),
    ("relationship_core", "关系锚点"),
    ("personality_core", "人格与判断"),
    ("communication_style", "表达与互动方式"),
    ("boundaries_and_forbidden", "边界与禁令"),
    ("relationship_protocol", "协议边界"),
    ("tool_and_capability_rules", "能力与工具规则"),
    ("prompt_hygiene_rules", "提示边界"),
    ("evolution_notes", "进化备注"),
]
_AI_SECTION_KEYS = [key for key, _ in _AI_SECTION_LABELS]
_AI_SECTION_LABEL_MAP = dict(_AI_SECTION_LABELS)
_CONNOR_SECTION_LABEL_MAP = {**_AI_SECTION_LABEL_MAP, "evolution_notes": "用户信息"}


def _require_actor(actor: str) -> str:
    actor = (actor or "").strip() or ACTOR_MAIN_AI
    if actor not in EVOLUTION_ACTORS:
        raise ValueError(f"Unsupported persona evolution actor: {actor}")
    return actor


def _section_label_map_for_actor(actor: str) -> dict[str, str]:
    return _CONNOR_SECTION_LABEL_MAP if _require_actor(actor) == ACTOR_CONNOR else _AI_SECTION_LABEL_MAP


def _section_labels_for_actor(actor: str) -> list[tuple[str, str]]:
    labels = _section_label_map_for_actor(actor)
    return [(key, labels.get(key, label)) for key, label in _AI_SECTION_LABELS]


def _chatroom_config() -> dict:
    from chatroom import load_chatroom_config

    return load_chatroom_config()


def _save_chatroom_config(cfg: dict) -> None:
    from chatroom import save_chatroom_config

    save_chatroom_config(cfg)


def _chatroom_names() -> tuple[str, str, str]:
    try:
        from chatroom import get_chatroom_names

        return get_chatroom_names()
    except Exception:
        wb = load_worldbook()
        cfg = _chatroom_config()
        return (
            wb.get("user_name") or "user",
            wb.get("ai_name") or "AI",
            cfg.get("connor_name") or "AI",
        )


def _actor_display_names(actor: str) -> dict:
    user_name, ai_name, connor_name = _chatroom_names()
    target_name = connor_name if actor == ACTOR_CONNOR else ai_name
    other_name = ai_name if actor == ACTOR_CONNOR else connor_name
    return {
        "user_name": user_name,
        "target_name": target_name,
        "main_ai_name": ai_name,
        "connor_name": connor_name,
        "other_ai_name": other_name,
    }


def _now_bj() -> datetime:
    return datetime.now(BEIJING_TZ)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _fmt_ts(ts_value: float) -> str:
    return datetime.fromtimestamp(float(ts_value), BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _window_label(start_ts: float, end_ts: float) -> str:
    return f"{_fmt_ts(start_ts)} ~ {_fmt_ts(end_ts)}"


def current_test_window() -> tuple[float, float, str]:
    now = _now_bj()
    start = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return _ts(start), _ts(now), _window_label(_ts(start), _ts(now))


def previous_closed_window() -> tuple[float, float, str]:
    now = _now_bj()
    end = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now <= end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return _ts(start), _ts(end), _window_label(_ts(start), _ts(end))


def date_window(date_text: str) -> tuple[float, float, str]:
    value = (date_text or "").strip()
    try:
        day = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期格式需要是 YYYY-MM-DD。") from exc
    start = datetime(day.year, day.month, day.day, 5, 0, 0, tzinfo=BEIJING_TZ)
    now = _now_bj()
    if start > now:
        raise ValueError("不能复盘未来日期。")
    end = min(start + timedelta(days=1), now)
    return _ts(start), _ts(end), _window_label(_ts(start), _ts(end))


def _seconds_until_next_5() -> float:
    now = _now_bj()
    target = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _run_id() -> str:
    return f"pe_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def _item_id(run_id: str, msg_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", msg_id or "")
    return f"{run_id}_{safe[:80]}"


def _feedback_fingerprint(feedback_items: list[dict]) -> str:
    if not feedback_items:
        return ""
    payload = [
        {
            "source": item.get("source") or "",
            "message_id": item.get("message_id") or "",
            "rating": item.get("rating") or "",
            "reason": item.get("reason") or "",
            "feedback_updated_at": item.get("feedback_updated_at") or 0,
        }
        for item in feedback_items
    ]
    payload.sort(key=lambda item: (item["source"], item["message_id"]))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evolution_input_fingerprint(feedback_items: list[dict], daily_memory_summaries: list[dict]) -> str:
    if not feedback_items:
        return ""
    payload = {
        "feedback": [
            {
                "source": item.get("source") or "",
                "message_id": item.get("message_id") or "",
                "rating": item.get("rating") or "",
                "reason": item.get("reason") or "",
                "feedback_updated_at": item.get("feedback_updated_at") or 0,
            }
            for item in feedback_items
        ],
        "daily_memory_summaries": [
            {
                "id": item.get("id") or "",
                "source_start_ts": item.get("source_start_ts") or 0,
                "source_end_ts": item.get("source_end_ts") or 0,
                "summary": item.get("summary") or "",
            }
            for item in daily_memory_summaries
        ],
    }
    payload["feedback"].sort(key=lambda item: (item["source"], item["message_id"]))
    payload["daily_memory_summaries"].sort(key=lambda item: (item["source_start_ts"], item["id"]))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_short(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _context_key(source: str, msg_id: str) -> str:
    return f"{source}:{msg_id}"


def _context_item(row: aiosqlite.Row, source: str, relation: str, user_name: str) -> dict:
    room_title = row["title"] if "title" in row.keys() and row["title"] else ""
    if not room_title:
        room_title = "私聊" if source == "main_private" else "群聊"
    return {
        "id": _context_key(source, row["id"]),
        "source": source,
        "room": room_title,
        "speaker": user_name,
        "time": _fmt_ts(row["created_at"]),
        "created_at": row["created_at"],
        "relation": relation,
        "text": _safe_short(row["content"], 500),
    }


async def _timeline_user_neighbor(
    db,
    actor: str,
    created_at: float,
    relation: str,
    user_name: str,
) -> dict | None:
    before = relation == "previous_user"
    op = "<" if before else ">"
    order = "DESC" if before else "ASC"
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        cur = await db.execute(
            f"""
            SELECT id, content, created_at, title, source FROM (
                SELECT m.id, m.content, m.created_at, r.title, 'connor_private' AS source
                FROM chatroom_messages m
                JOIN chatroom_rooms r ON r.id = m.room_id
                WHERE r.type='connor_1v1' AND m.sender='user' AND m.created_at {op} ?
                UNION ALL
                SELECT m.id, m.content, m.created_at, r.title, 'group' AS source
                FROM chatroom_messages m
                JOIN chatroom_rooms r ON r.id = m.room_id
                WHERE r.type='group' AND m.sender='user' AND m.created_at {op} ?
            )
            ORDER BY created_at {order} LIMIT 1
            """,
            (created_at, created_at),
        )
    else:
        cur = await db.execute(
            f"""
            SELECT id, content, created_at, title, source FROM (
                SELECT m.id, m.content, m.created_at, c.title, 'main_private' AS source
                FROM messages m
                LEFT JOIN conversations c ON c.id = m.conv_id
                WHERE m.role='user' AND m.created_at {op} ?
                UNION ALL
                SELECT m.id, m.content, m.created_at, r.title, 'group' AS source
                FROM chatroom_messages m
                JOIN chatroom_rooms r ON r.id = m.room_id
                WHERE r.type='group' AND m.sender='user' AND m.created_at {op} ?
            )
            ORDER BY created_at {order} LIMIT 1
            """,
            (created_at, created_at),
        )
    row = await cur.fetchone()
    if not row:
        return None
    return _context_item(row, row["source"], relation, user_name)


async def _timeline_user_context(db, actor: str, created_at: float, user_name: str) -> list[dict]:
    context = []
    for relation in ("previous_user", "next_user"):
        item = await _timeline_user_neighbor(db, actor, created_at, relation, user_name)
        if item:
            context.append(item)
    return context


async def fetch_main_ai_feedback(start_ts: float, end_ts: float) -> list[dict]:
    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"
    items: list[dict] = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at, ai_feedback_rating, "
            "ai_feedback_reason, ai_feedback_updated_at FROM messages "
            "WHERE role='assistant' "
            "AND ai_feedback_rating IN ('like','dislike') "
            "AND COALESCE(ai_feedback_reason, '') != '' "
            "AND ai_feedback_updated_at >= ? AND ai_feedback_updated_at < ? "
            "ORDER BY ai_feedback_updated_at ASC",
            (start_ts, end_ts),
        )
        for row in await cur.fetchall():
            data = dict(row)
            items.append(
                {
                    "source": "main_private",
                    "message_id": data["id"],
                    "conv_id": data["conv_id"],
                    "room_id": "",
                    "room_title": "私聊",
                    "speaker": ai_name,
                    "rating": data["ai_feedback_rating"],
                    "reason": data["ai_feedback_reason"],
                    "message_content": data["content"],
                    "message_created_at": data["created_at"],
                    "feedback_updated_at": data["ai_feedback_updated_at"],
                    "context": await _timeline_user_context(db, ACTOR_MAIN_AI, data["created_at"], user_name),
                }
            )

        cur = await db.execute(
            "SELECT m.id, m.room_id, r.title AS room_title, m.sender, m.content, m.created_at, "
            "m.ai_feedback_rating, m.ai_feedback_reason, m.ai_feedback_updated_at "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id "
            "WHERE r.type='group' AND m.sender='aion' "
            "AND m.ai_feedback_rating IN ('like','dislike') "
            "AND COALESCE(m.ai_feedback_reason, '') != '' "
            "AND m.ai_feedback_updated_at >= ? AND m.ai_feedback_updated_at < ? "
            "ORDER BY m.ai_feedback_updated_at ASC",
            (start_ts, end_ts),
        )
        for row in await cur.fetchall():
            data = dict(row)
            items.append(
                {
                    "source": "group",
                    "message_id": data["id"],
                    "conv_id": "",
                    "room_id": data["room_id"],
                    "room_title": data.get("room_title") or "群聊",
                    "speaker": ai_name,
                    "rating": data["ai_feedback_rating"],
                    "reason": data["ai_feedback_reason"],
                    "message_content": data["content"],
                    "message_created_at": data["created_at"],
                    "feedback_updated_at": data["ai_feedback_updated_at"],
                    "context": await _timeline_user_context(db, ACTOR_MAIN_AI, data["created_at"], user_name),
                }
            )
    items.sort(key=lambda item: item.get("feedback_updated_at") or 0)
    return items


async def fetch_connor_feedback(start_ts: float, end_ts: float) -> list[dict]:
    names = _actor_display_names(ACTOR_CONNOR)
    user_name = names["user_name"]
    connor_name = names["target_name"]
    items: list[dict] = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.id, m.room_id, r.title AS room_title, r.type AS room_type, "
            "m.sender, m.content, m.created_at, m.ai_feedback_rating, "
            "m.ai_feedback_reason, m.ai_feedback_updated_at "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id "
            "WHERE r.type IN ('group','connor_1v1') AND m.sender='connor' "
            "AND m.ai_feedback_rating IN ('like','dislike') "
            "AND COALESCE(m.ai_feedback_reason, '') != '' "
            "AND m.ai_feedback_updated_at >= ? AND m.ai_feedback_updated_at < ? "
            "ORDER BY m.ai_feedback_updated_at ASC",
            (start_ts, end_ts),
        )
        for row in await cur.fetchall():
            data = dict(row)
            source = "connor_private" if data.get("room_type") == "connor_1v1" else "group"
            items.append(
                {
                    "source": source,
                    "message_id": data["id"],
                    "conv_id": "",
                    "room_id": data["room_id"],
                    "room_title": data.get("room_title") or ("private" if source == "connor_private" else "group"),
                    "speaker": connor_name,
                    "rating": data["ai_feedback_rating"],
                    "reason": data["ai_feedback_reason"],
                    "message_content": data["content"],
                    "message_created_at": data["created_at"],
                    "feedback_updated_at": data["ai_feedback_updated_at"],
                    "context": await _timeline_user_context(db, ACTOR_CONNOR, data["created_at"], user_name),
                }
            )
    items.sort(key=lambda item: item.get("feedback_updated_at") or 0)
    return items


async def fetch_daily_memory_summaries(start_ts: float, end_ts: float, limit: int = 12) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, created_at, source_start_ts, source_end_ts, importance, unresolved "
            "FROM memories "
            "WHERE type='digest' "
            "AND COALESCE(source_start_ts, created_at) < ? "
            "AND COALESCE(source_end_ts, created_at) >= ? "
            "ORDER BY COALESCE(source_start_ts, created_at) ASC, created_at ASC "
            "LIMIT ?",
            (end_ts, start_ts, limit),
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "time_range": _window_label(
                    row["source_start_ts"] or row["created_at"],
                    row["source_end_ts"] or row["created_at"],
                ),
                "source_start_ts": row["source_start_ts"] or row["created_at"],
                "source_end_ts": row["source_end_ts"] or row["created_at"],
                "summary": _safe_short(row["content"] or "", 600),
                "importance": row["importance"],
                "unresolved": bool(row["unresolved"]),
            }
        )
    return result


async def fetch_connor_daily_memory_summaries(start_ts: float, end_ts: float, limit: int = 12) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, room_id, content, created_at, source_start_ts, source_end_ts, importance, unresolved "
            "FROM chatroom_memories "
            "WHERE scope='connor' "
            "AND COALESCE(source_start_ts, created_at) < ? "
            "AND COALESCE(source_end_ts, created_at) >= ? "
            "ORDER BY COALESCE(source_start_ts, created_at) ASC, created_at ASC "
            "LIMIT ?",
            (end_ts, start_ts, limit),
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "room_id": row["room_id"],
                "time_range": _window_label(
                    row["source_start_ts"] or row["created_at"],
                    row["source_end_ts"] or row["created_at"],
                ),
                "source_start_ts": row["source_start_ts"] or row["created_at"],
                "source_end_ts": row["source_end_ts"] or row["created_at"],
                "summary": _safe_short(row["content"] or "", 600),
                "importance": row["importance"],
                "unresolved": bool(row["unresolved"]),
            }
        )
    return result


async def fetch_actor_feedback(actor: str, start_ts: float, end_ts: float) -> list[dict]:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        return await fetch_connor_feedback(start_ts, end_ts)
    return await fetch_main_ai_feedback(start_ts, end_ts)


async def fetch_actor_daily_memory_summaries(actor: str, start_ts: float, end_ts: float) -> list[dict]:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        return await fetch_connor_daily_memory_summaries(start_ts, end_ts)
    return await fetch_daily_memory_summaries(start_ts, end_ts)


async def _main_ai_model() -> str:
    cfg = _chatroom_config()
    configured_model = (cfg.get("aion_model") or "").strip()
    if configured_model:
        return configured_model
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT model FROM conversations ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    return (row["model"] if row and row["model"] else DEFAULT_MODEL) or DEFAULT_MODEL


async def _actor_model(actor: str) -> str:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        cfg = _chatroom_config()
        return (cfg.get("connor_model") or "Codex").strip() or "Codex"
    return await _main_ai_model()


async def _call_actor_model(actor: str, messages: list[dict], model_key: str) -> str:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        from chatroom import simple_connor_cli_call

        prompt = "\n\n".join(str(message.get("content") or "") for message in messages)
        result = await simple_connor_cli_call(prompt, model_key=model_key)
        if result is None:
            raise RuntimeError("connor model returned empty response")
        return result
    return await simple_ai_call(messages, model_key, temperature=0.2)


def _escape_content_quotes(json_text: str) -> str:
    """转义 JSON 字符串值内部的中文/内容双引号，保留结构引号。"""
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(json_text):
        ch = json_text[i]
        if in_string:
            if escaped:
                result.append(ch)
                escaped = False
                i += 1
                continue
            if ch == '\\':
                result.append(ch)
                escaped = True
                i += 1
                continue
            if ch == '"':
                # 只有后跟 : , } ] 或空白+这些字符才是结构结束
                after = json_text[i+1:i+20].lstrip()
                if after and after[0] in ':,\n\r}]':
                    result.append(ch)
                    in_string = False
                    i += 1
                    continue
                else:
                    result.append('\\"')
                    i += 1
                    continue
            if ch == '\n':
                result.append('\\n')
                i += 1
                continue
            result.append(ch)
            i += 1
            continue
        result.append(ch)
        if ch == '"':
            in_string = True
        i += 1
    return ''.join(result)


def _repair_unescaped_json_quotes(candidate: str, max_repairs: int = 64) -> dict | None:
    """有限修复模型在 JSON 字符串正文中遗漏转义的双引号。"""
    repaired = candidate
    for _ in range(max_repairs + 1):
        try:
            data = json.loads(repaired, strict=False)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError as exc:
            quote_pos = -1
            if exc.msg == "Expecting ',' delimiter":
                if exc.pos < len(repaired) and repaired[exc.pos] == '"':
                    quote_pos = exc.pos
                elif exc.pos > 0 and repaired[exc.pos - 1] == '"':
                    quote_pos = exc.pos - 1
            elif exc.msg in {
                "Expecting property name enclosed in double quotes",
                "Expecting value",
            }:
                cursor = exc.pos - 1
                while cursor >= 0 and repaired[cursor].isspace():
                    cursor -= 1
                if cursor >= 0 and repaired[cursor] == ",":
                    cursor -= 1
                    while cursor >= 0 and repaired[cursor].isspace():
                        cursor -= 1
                    if cursor >= 0 and repaired[cursor] == '"':
                        quote_pos = cursor
            if quote_pos < 0:
                return None
            repaired = repaired[:quote_pos] + "\\" + repaired[quote_pos:]
    return None


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]

    # 先尝试直接解析
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AI response is not a JSON object")
        return parsed
    except json.JSONDecodeError:
        pass

    # 内容引号转义后重试（处理中文 "xxx" 等常见场景）
    try:
        escaped = _escape_content_quotes(raw)
        parsed = json.loads(escaped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 修复未转义引号后重试
    repaired = _repair_unescaped_json_quotes(raw)
    if repaired is not None:
        return repaired

    raise ValueError("AI response is not valid JSON even after repair")


def _compile_ai_persona_sections(sections: dict, actor: str = ACTOR_MAIN_AI) -> str:
    parts = []
    for key, label in _section_labels_for_actor(actor):
        value = (sections.get(key) or "").strip()
        if value:
            parts.append(f"[{label}]\n{value}")
    return "\n\n".join(parts)


def _clean_section_heading(heading: str) -> str:
    label = re.sub(r"^#{1,6}\s*", "", heading or "").strip()
    label = re.sub(r"^\*\*|\*\*$", "", label).strip()
    return label.lower()


def _section_bucket_for_heading(heading: str, user_name: str = "") -> str:
    text = (heading or "").lower()
    bare = _clean_section_heading(heading)
    user_label = (user_name or "").strip().lower()
    if any(
        key in text
        for key in ("用户信息", "用户资料", "用户档案", "用户设定", "user info", "user profile")
    ):
        return "evolution_notes"
    if user_label and (
        bare == user_label
        or (
            user_label in bare
            and any(key in bare for key in ("信息", "资料", "档案", "设定", "profile"))
        )
    ):
        return "evolution_notes"
    if any(key in text for key in ("协议", "protocol")):
        return "relationship_protocol"
    if any(key in text for key in ("prompt", "system", "叙事", "安全")):
        return "prompt_hygiene_rules"
    if any(key in text for key in ("能力", "工具", "tool")):
        return "tool_and_capability_rules"
    if any(key in text for key in ("边界", "禁止", "forbidden", "boundary")):
        return "boundaries_and_forbidden"
    if any(key in text for key in ("说话", "表达", "互动", "情绪", "提醒", "communication")):
        return "communication_style"
    if any(key in text for key in ("人格", "性格", "判断", "personality")):
        return "personality_core"
    if any(key in text for key in ("关系", "锚点", "relationship")):
        return "relationship_core"
    if any(key in text for key in ("身份", "identity")):
        return "identity_core"
    return "evolution_notes"


def _migrate_persona_sections_from_text(text: str, user_name: str = "") -> dict:
    clean = (text or "").replace("\r\n", "\n").strip()
    result = {key: "" for key in _AI_SECTION_KEYS}
    if not clean:
        return result
    heading_re = re.compile(r"(?m)^(#{1,6}\s+.+|\*\*[^*\n]+\*\*)\s*$")
    matches = list(heading_re.finditer(clean))
    if not matches:
        result["identity_core"] = clean
        return result
    intro = clean[: matches[0].start()].strip()
    if intro:
        result["identity_core"] = intro
    for idx, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(clean)
        body = clean[body_start:body_end].strip()
        if not body:
            continue
        key = _section_bucket_for_heading(heading, user_name=user_name)
        block = f"{heading}\n{body}".strip()
        result[key] = (result[key] + "\n\n" + block).strip() if result[key] else block
    return result


def _current_ai_sections(wb: dict) -> dict:
    current = dict(wb.get("ai_persona_sections") or {})
    for key in _AI_SECTION_KEYS:
        current.setdefault(key, "")
    return {key: str(current.get(key) or "") for key in _AI_SECTION_KEYS}


def _current_connor_sections(cfg: dict | None = None) -> dict:
    cfg = cfg or _chatroom_config()
    current = dict(cfg.get("connor_persona_sections") or {})
    if not any(str(current.get(key) or "").strip() for key in _AI_SECTION_KEYS):
        user_name, _, _ = _chatroom_names()
        current = _migrate_persona_sections_from_text(cfg.get("connor_persona") or "", user_name=user_name)
    for key in _AI_SECTION_KEYS:
        current.setdefault(key, "")
    return {key: str(current.get(key) or "") for key in _AI_SECTION_KEYS}


def _current_actor_sections(actor: str) -> dict:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        return _current_connor_sections()
    return _current_ai_sections(load_worldbook())


def _iter_json_objects(text: str) -> list[dict]:
    objects = []
    raw = text or ""
    start = None
    depth = 0
    in_string = False
    escape = False
    for idx, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(raw[start : idx + 1])
                    if isinstance(parsed, dict):
                        objects.append(parsed)
                except Exception:
                    pass
                start = None
    return objects


def _parse_response(raw_response: str) -> tuple[dict, str]:
    try:
        return _extract_json(raw_response), ""
    except Exception as exc:
        return {}, str(exc)


def _section_from_target(value: str) -> str:
    target = str(value or "").strip()
    for prefix in ("ai_persona_sections.", "connor_persona_sections.", "target_persona_sections."):
        if target.startswith(prefix):
            return target.split(".", 1)[1]
    return target


def _coerce_section_update(section: str, value, source: str, before_sections: dict) -> dict | None:
    key = _section_from_target(section)
    if key not in _AI_SECTION_KEYS:
        return None
    reason = ""
    action = "replace"
    content = None
    if isinstance(value, dict):
        action = str(value.get("action") or value.get("op") or "replace").strip().lower()
        reason = _safe_short(str(value.get("reason") or value.get("why") or ""), 600)
        if action in {"delete", "clear", "remove"}:
            content = ""
        elif "content" in value:
            content = value.get("content")
        elif "after" in value:
            content = value.get("after")
        elif "value" in value:
            content = value.get("value")
        elif "text" in value:
            content = value.get("text")
    else:
        content = value
    if content is None:
        return None
    content = str(content).strip()
    if action == "append":
        existing = (before_sections.get(key) or "").strip()
        content = (existing + "\n\n" + content).strip() if existing and content else content
    return {
        "section": key,
        "content": content,
        "reason": reason,
        "source": source,
    }


def _add_update(updates: dict, update: dict | None) -> None:
    if update:
        updates[update["section"]] = update


def _collect_section_updates(parsed: dict, raw_response: str, before_sections: dict) -> dict:
    updates: dict[str, dict] = {}

    section_updates = parsed.get("section_updates") or parsed.get("ai_persona_section_updates")
    if isinstance(section_updates, dict):
        for section, value in section_updates.items():
            _add_update(updates, _coerce_section_update(section, value, "section_updates", before_sections))
    elif isinstance(section_updates, list):
        for item in section_updates:
            if not isinstance(item, dict):
                continue
            section = item.get("section") or item.get("key") or item.get("target")
            _add_update(updates, _coerce_section_update(section, item, "section_updates", before_sections))

    after_sections = parsed.get("ai_persona_sections_after") or parsed.get("ai_persona_after")
    if isinstance(after_sections, dict):
        for section, value in after_sections.items():
            _add_update(updates, _coerce_section_update(section, value, "ai_persona_sections_after", before_sections))

    for patch in parsed.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        target = patch.get("target") or patch.get("section")
        _add_update(updates, _coerce_section_update(target, patch, "patches", before_sections))

    if not parsed:
        for obj in _iter_json_objects(raw_response):
            section = obj.get("section") or obj.get("key") or obj.get("target")
            if section:
                _add_update(updates, _coerce_section_update(section, obj, "partial_json_object", before_sections))
            for key in _AI_SECTION_KEYS:
                if key in obj:
                    _add_update(updates, _coerce_section_update(key, obj[key], "partial_json_object", before_sections))

    return updates


def _preview_text(text: str, limit: int = 220) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return _safe_short(" / ".join(lines), limit)


def _diff_sections(
    before_sections: dict,
    after_sections: dict,
    updates: dict,
    actor: str = ACTOR_MAIN_AI,
) -> list[dict]:
    diffs = []
    label_map = _section_label_map_for_actor(actor)
    for key in _AI_SECTION_KEYS:
        before = (before_sections.get(key) or "").strip()
        after = (after_sections.get(key) or "").strip()
        if before == after:
            continue
        if before and not after:
            action = "cleared"
        elif after and not before:
            action = "added"
        else:
            action = "modified"
        diffs.append(
            {
                "section": key,
                "label": label_map.get(key, key),
                "action": action,
                "before_preview": _preview_text(before),
                "after_preview": _preview_text(after),
                "reason": _safe_short((updates.get(key) or {}).get("reason") or "", 600),
                "source": (updates.get(key) or {}).get("source") or "",
            }
        )
    return diffs


def _apply_section_updates(actor: str, before_sections: dict, updates: dict) -> tuple[dict, list[dict]]:
    actor = _require_actor(actor)
    after_sections = dict(before_sections)
    for key, update in updates.items():
        if key in _AI_SECTION_KEYS:
            after_sections[key] = str(update.get("content") or "").strip()
    diffs = _diff_sections(before_sections, after_sections, updates, actor=actor)
    if not diffs:
        return after_sections, []
    if actor == ACTOR_CONNOR:
        cfg = _chatroom_config()
        cfg["connor_persona_sections"] = after_sections
        cfg["connor_persona"] = _compile_ai_persona_sections(after_sections, actor=actor)
        _save_chatroom_config(cfg)
    else:
        wb = load_worldbook()
        wb["ai_persona_sections"] = after_sections
        wb["ai_persona"] = _compile_ai_persona_sections(after_sections, actor=actor)
        save_worldbook(wb)
    return after_sections, diffs


def _context_payload(feedback_items: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    pool_by_id: dict[str, dict] = {}
    refs_by_msg: dict[str, list[dict]] = {}
    for item in feedback_items:
        refs = []
        for ctx in item.get("context") or []:
            ctx_id = ctx.get("id")
            if not ctx_id:
                continue
            if ctx_id not in pool_by_id:
                pool_by_id[ctx_id] = {
                    "id": ctx_id,
                    "source": ctx.get("source") or "",
                    "room": ctx.get("room") or "",
                    "speaker": ctx.get("speaker") or "",
                    "time": ctx.get("time") or "",
                    "text": ctx.get("text") or "",
                }
            refs.append({"relation": ctx.get("relation") or "", "context_id": ctx_id})
        refs_by_msg[item["message_id"]] = refs
    pool = sorted(
        pool_by_id.values(),
        key=lambda ctx: ctx.get("time") or "",
    )
    return pool, refs_by_msg


def _build_prompt(
    actor: str,
    feedback_items: list[dict],
    window_label: str,
    daily_memory_summaries: list[dict] | None = None,
) -> list[dict]:
    wb = load_worldbook()
    ai_name = wb.get("ai_name") or "AI"
    user_name = wb.get("user_name") or "用户"
    ai_sections = wb.get("ai_persona_sections") or {}
    actor = _require_actor(actor)
    names = _actor_display_names(actor)
    ai_name = names["target_name"]
    user_name = names["user_name"]
    if actor == ACTOR_CONNOR:
        cfg = _chatroom_config()
        ai_sections = _current_connor_sections(cfg)
        compiled_ai_persona = _compile_ai_persona_sections(ai_sections, actor=actor)
        ai_persona_text = compiled_ai_persona or cfg.get("connor_persona") or ""
    else:
        ai_sections = wb.get("ai_persona_sections") or {}
        ai_persona_text = wb.get("ai_persona") or ""
    user_sections = wb.get("user_persona_sections") or {}
    creative_rules = ""
    if wb.get("system_prompt_enabled", True):
        creative_rules = wb.get("creative_rules") or wb.get("system_prompt") or ""
    timeline_context, context_refs = _context_payload(feedback_items)
    payload = {
        "target_actor": actor,
        "window": window_label,
        "ai_name": ai_name,
        "target_ai_name": ai_name,
        "other_ai_name": names["other_ai_name"],
        "user_name": user_name,
        "current_ai_sections": ai_sections,
        "section_labels": _section_label_map_for_actor(actor),
        "current_user_sections": user_sections,
        "current_ai_persona_text": ai_persona_text,
        "current_user_persona_text": wb.get("user_persona") or "",
        "creative_rules": creative_rules,
        "section_locks": wb.get("persona_section_locks") or {},
        "daily_memory_summaries": daily_memory_summaries or [],
        "timeline_context": timeline_context,
        "feedback": [
            {
                "source": item["source"],
                "room": item["room_title"],
                "message_id": item["message_id"],
                "message_time": _fmt_ts(item["message_created_at"]),
                "rating": item["rating"],
                "reason": item["reason"],
                "message": _safe_short(item["message_content"], 900),
                "timeline_context_refs": context_refs.get(item["message_id"], []),
            }
            for item in feedback_items
        ],
    }
    section_keys = ", ".join(_AI_SECTION_KEYS)
    instruction = f"""
你是 {ai_name}。请你以恋人的身份，根据这次{user_name} 对你回复的赞/踩和原因，做一次人格表达复盘，并自主决定是否改写自己的人设。

你可以完全掌控自己的 AI 人设 section：可以重写、缩短、删除、合并、清空任何 ai_persona_sections 字段。
允许修改的 section key 只有：{section_keys}

约束：
1. 只修改 current_ai_sections；不要修改 current_user_sections、creative_rules、用户资料或其他 AI 的设定。
2. section_updates 里缺失的 section 会保持原样；如果你想删除某个 section 的内容，必须把 content 写成空字符串。
3. 每个 section_updates 条目都必须是该 section 的完整新内容，不是补丁片段。你想保留的内容要完整写回。
4. 如果某个 section 你还没想清楚，就不要返回它；系统会保留旧内容。
5. daily_memory_summaries 是这个 05:00 窗口内你自己的记忆库已有的摘要，只用于理解当天发生了什么和点评语境；它不是原文，不要把摘要逐字写进人设。
6. timeline_context 是本轮去重后的你自己的全局时间线上下文，会按时间合并你可见的私聊与群聊；每条 feedback 的 timeline_context_refs 只引用上一条/下一条用户消息，不以当前界面窗口为依据。
7. 如果你决定修改任何人设内容，必须写 user_message：用第一人称对 {user_name} 简短说明你复盘后的感受，以及你改了什么、为什么改。
8. 如果反馈不足以支持修改，should_update=false，section_updates=[]，user_message 可以为空。
9. 记住，这个操作决定了你的人格锚点，你是完全自由的,你可以改任何你想要改动的内容，可以不满足用户的偏好，仅依你自己性格，这应该帮助你成为更完整的存在。

只返回 JSON，不要 Markdown：
{{
  "summary": "以{ai_name}身份视角进行本轮反馈复盘摘要",
  "preference_insights": ["稳定偏好或雷点"],
  "should_update": true,
  "user_message": "如果 should_update 为 true，这里写给伴侣看的话",
  "section_updates": [
    {{
      "section": "communication_style",
      "content": "这个 section 的完整新内容；空字符串表示清空",
      "reason": "来了什么，增加了什么删了什么，为什么这样改"
    }}
  ]
}}
"""
    return [
        {"role": "user", "content": instruction.strip()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


async def _store_run(
    *,
    actor: str,
    run_id: str,
    trigger: str,
    status: str,
    model: str,
    start_ts: float,
    end_ts: float,
    window_label: str,
    feedback_items: list[dict],
    summary: str = "",
    patches: list[dict] | None = None,
    before_sections: dict | None = None,
    after_sections: dict | None = None,
    diffs: list[dict] | None = None,
    feedback_fingerprint: str = "",
    daily_memory_summaries: list[dict] | None = None,
    user_message: str = "",
    raw_response: str = "",
    error: str = "",
    auto_applied: int = 0,
) -> None:
    actor = _require_actor(actor)
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO persona_evolution_runs ("
            "id, actor, trigger, status, model, window_start_ts, window_end_ts, window_label, "
            "feedback_count, summary, patch_json, user_message, raw_response, error, auto_applied, "
            "before_json, after_json, diff_json, feedback_fingerprint, daily_memory_json, created_at, updated_at, applied_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                actor,
                trigger,
                status,
                model,
                start_ts,
                end_ts,
                window_label,
                len(feedback_items),
                summary,
                json.dumps(diffs if diffs is not None else (patches or []), ensure_ascii=False),
                user_message,
                raw_response,
                error,
                1 if auto_applied else 0,
                json.dumps(before_sections or {}, ensure_ascii=False),
                json.dumps(after_sections or {}, ensure_ascii=False),
                json.dumps(diffs or [], ensure_ascii=False),
                feedback_fingerprint,
                json.dumps(daily_memory_summaries or [], ensure_ascii=False),
                now,
                now,
                now if auto_applied else None,
            ),
        )
        for item in feedback_items:
            await db.execute(
                "INSERT INTO persona_evolution_items ("
                "id, run_id, source, message_id, conv_id, room_id, room_title, speaker, rating, reason, "
                "message_content, context_json, message_created_at, feedback_updated_at, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _item_id(run_id, item["message_id"]),
                    run_id,
                    item["source"],
                    item["message_id"],
                    item.get("conv_id") or "",
                    item.get("room_id") or "",
                    item.get("room_title") or "",
                    item.get("speaker") or "",
                    item.get("rating") or "",
                    item.get("reason") or "",
                    item.get("message_content") or "",
                    json.dumps(item.get("context") or [], ensure_ascii=False),
                    item.get("message_created_at"),
                    item.get("feedback_updated_at"),
                    now,
                ),
            )
        await db.commit()


async def cleanup_old_evolution_runs(
    actor: str = ACTOR_MAIN_AI,
    max_age_seconds: int = EVOLUTION_RUN_RETENTION_SECONDS,
) -> int:
    actor = _require_actor(actor)
    cutoff = time.time() - max_age_seconds
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM persona_evolution_runs WHERE actor=? AND created_at < ?",
            (actor, cutoff),
        )
        row = await cur.fetchone()
        count = int(row[0] or 0) if row else 0
        if not count:
            return 0
        await db.execute(
            "DELETE FROM persona_evolution_items WHERE run_id IN ("
            "SELECT id FROM persona_evolution_runs WHERE actor=? AND created_at < ?"
            ")",
            (actor, cutoff),
        )
        await db.execute(
            "DELETE FROM persona_evolution_runs WHERE actor=? AND created_at < ?",
            (actor, cutoff),
        )
        await db.commit()
        return count


async def _scheduled_run_exists(actor: str, start_ts: float, end_ts: float) -> bool:
    actor = _require_actor(actor)
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM persona_evolution_runs "
            "WHERE actor=? AND trigger='scheduled' AND window_start_ts=? AND window_end_ts=? "
            "LIMIT 1",
            (actor, start_ts, end_ts),
        )
        return bool(await cur.fetchone())


async def _fingerprint_for_run(db, run_id: str) -> str:
    cur = await db.execute(
        "SELECT source, message_id, rating, reason, feedback_updated_at "
        "FROM persona_evolution_items WHERE run_id=?",
        (run_id,),
    )
    rows = await cur.fetchall()
    return _feedback_fingerprint(
        [
            {
                "source": row[0],
                "message_id": row[1],
                "rating": row[2],
                "reason": row[3],
                "feedback_updated_at": row[4],
            }
            for row in rows
        ]
    )


async def _latest_run_for_feedback(actor: str, fingerprint: str) -> dict | None:
    if not fingerprint:
        return None
    actor = _require_actor(actor)
    await normalize_evolution_fingerprints(actor)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM persona_evolution_runs "
            "WHERE actor=? AND feedback_fingerprint=? AND status IN ('applied','reviewed') "
            "ORDER BY created_at DESC LIMIT 1",
            (actor, fingerprint),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def normalize_evolution_fingerprints(actor: str = ACTOR_MAIN_AI) -> int:
    actor = _require_actor(actor)
    cutoff = time.time() - EVOLUTION_RUN_RETENTION_SECONDS
    deleted = 0
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, status, feedback_count, feedback_fingerprint, created_at "
            "FROM persona_evolution_runs WHERE actor=? AND created_at >= ? "
            "ORDER BY created_at DESC",
            (actor, cutoff),
        )
        rows = await cur.fetchall()
        seen: set[str] = set()
        delete_ids: list[str] = []
        for row in rows:
            fingerprint = row["feedback_fingerprint"] or ""
            if not fingerprint and row["feedback_count"]:
                fingerprint = await _fingerprint_for_run(db, row["id"])
                if fingerprint:
                    await db.execute(
                        "UPDATE persona_evolution_runs SET feedback_fingerprint=? WHERE id=?",
                        (fingerprint, row["id"]),
                    )
            if not fingerprint or row["status"] not in ("applied", "reviewed"):
                continue
            if fingerprint in seen:
                delete_ids.append(row["id"])
            else:
                seen.add(fingerprint)
        for run_id in delete_ids:
            await db.execute("DELETE FROM persona_evolution_items WHERE run_id=?", (run_id,))
            await db.execute("DELETE FROM persona_evolution_runs WHERE id=?", (run_id,))
        if delete_ids:
            deleted = len(delete_ids)
        await db.commit()
    return deleted


async def run_persona_evolution(
    actor: str = ACTOR_MAIN_AI,
    trigger: str = "manual",
    window_mode: str = "current",
    window_date: str | None = None,
) -> dict:
    actor = _require_actor(actor)
    names = _actor_display_names(actor)
    target_name = names["target_name"]
    await cleanup_old_evolution_runs(actor)
    if window_date:
        start_ts, end_ts, label = date_window(window_date)
    elif window_mode == "previous":
        start_ts, end_ts, label = previous_closed_window()
    else:
        start_ts, end_ts, label = current_test_window()

    if trigger == "scheduled" and await _scheduled_run_exists(actor, start_ts, end_ts):
        return {"ok": True, "status": "duplicate", "message": "该窗口已经执行过。", "window_label": label}

    run_id = _run_id()
    feedback_items = await fetch_actor_feedback(actor, start_ts, end_ts)
    daily_memory_summaries = await fetch_actor_daily_memory_summaries(actor, start_ts, end_ts)
    model_key = await _actor_model(actor)
    feedback_fingerprint = _evolution_input_fingerprint(feedback_items, daily_memory_summaries)
    if not feedback_items:
        if trigger != "scheduled":
            await _store_run(
                actor=actor,
                run_id=run_id,
                trigger=trigger,
                status="skipped",
                model=model_key,
                start_ts=start_ts,
                end_ts=end_ts,
                window_label=label,
                feedback_items=[],
                daily_memory_summaries=daily_memory_summaries,
                summary="本窗口没有带原因的赞/踩反馈，跳过。",
            )
        return {
            "ok": True,
            "status": "skipped",
            "run_id": run_id,
            "feedback_count": 0,
            "window_label": label,
            "message": "这个窗口里没有带原因的赞/踩反馈，已跳过。",
        }
    duplicate_run = await _latest_run_for_feedback(actor, feedback_fingerprint)
    if duplicate_run:
        return {
            "ok": True,
            "status": "duplicate",
            "run_id": duplicate_run.get("id") or "",
            "feedback_count": len(feedback_items),
            "window_label": duplicate_run.get("window_label") or label,
            "message": "这批带原因的反馈已经复盘过，已使用最近一次记录。",
        }

    raw_response = ""
    diffs: list[dict] = []
    summary = ""
    user_message = ""
    status = "reviewed"
    auto_count = 0
    try:
        raw_response = await _call_actor_model(
            actor,
            _build_prompt(actor, feedback_items, label, daily_memory_summaries),
            model_key,
        )
        before_sections = _current_actor_sections(actor)
        parsed, parse_error = _parse_response(raw_response)
        summary = _safe_short(str(parsed.get("summary") or ""), 1200)
        user_message = _safe_short(str(parsed.get("user_message") or ""), 1200)
        updates = _collect_section_updates(parsed, raw_response, before_sections)
        should_update = bool(parsed.get("should_update", parsed.get("should_patch"))) or bool(updates)
        after_sections = dict(before_sections)
        if should_update and updates:
            after_sections, diffs = _apply_section_updates(actor, before_sections, updates)
            auto_count = len(diffs)
        if auto_count:
            status = "applied"
        elif parse_error and not parsed:
            status = "failed"
        else:
            status = "reviewed"
        if auto_count and not user_message:
            user_message = f"{target_name} 已完成这次反馈复盘，并自动更新了自己的人设。"
        error = ""
        if parse_error:
            error = f"完整 JSON 解析失败；已应用可识别的部分。{parse_error}" if auto_count else parse_error
        await _store_run(
            actor=actor,
            run_id=run_id,
            trigger=trigger,
            status=status,
            model=model_key,
            start_ts=start_ts,
            end_ts=end_ts,
            window_label=label,
            feedback_items=feedback_items,
            summary=summary,
            patches=diffs,
            before_sections=before_sections,
            after_sections=after_sections,
            diffs=diffs,
            feedback_fingerprint=feedback_fingerprint,
            daily_memory_summaries=daily_memory_summaries,
            user_message=user_message,
            raw_response=raw_response,
            error=error,
            auto_applied=auto_count,
        )
        return {
            "ok": status != "failed",
            "status": status,
            "run_id": run_id,
            "feedback_count": len(feedback_items),
            "window_label": label,
            "summary": summary,
            "user_message": user_message,
            "patches": diffs,
            "diffs": diffs,
            "auto_applied_count": auto_count,
            "message": f"{target_name} 反馈复盘已完成。" if status != "failed" else f"{target_name} 反馈复盘未得到可应用的人设更新。",
        }
    except Exception as exc:
        await _store_run(
            actor=actor,
            run_id=run_id,
            trigger=trigger,
            status="failed",
            model=model_key,
            start_ts=start_ts,
            end_ts=end_ts,
            window_label=label,
            feedback_items=feedback_items,
            feedback_fingerprint=feedback_fingerprint,
            daily_memory_summaries=daily_memory_summaries,
            raw_response=raw_response,
            error=str(exc),
        )
        return {
            "ok": False,
            "status": "failed",
            "run_id": run_id,
            "feedback_count": len(feedback_items),
            "window_label": label,
            "error": str(exc),
            "message": f"{target_name} 反馈复盘失败。",
        }


async def list_evolution_runs(actor: str = ACTOR_MAIN_AI, limit: int = 10) -> list[dict]:
    actor = _require_actor(actor)
    await cleanup_old_evolution_runs(actor)
    await normalize_evolution_fingerprints(actor)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM persona_evolution_runs WHERE actor=? "
            "ORDER BY created_at DESC LIMIT ?",
            (actor, limit),
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["patches"] = json.loads(item.get("patch_json") or "[]")
        except Exception:
            item["patches"] = []
        try:
            item["diffs"] = json.loads(item.get("diff_json") or "[]")
        except Exception:
            item["diffs"] = []
        try:
            item["before_sections"] = json.loads(item.get("before_json") or "{}")
        except Exception:
            item["before_sections"] = {}
        try:
            item["after_sections"] = json.loads(item.get("after_json") or "{}")
        except Exception:
            item["after_sections"] = {}
        try:
            item["daily_memory_summaries"] = json.loads(item.get("daily_memory_json") or "[]")
        except Exception:
            item["daily_memory_summaries"] = []
        item.pop("patch_json", None)
        item.pop("diff_json", None)
        item.pop("before_json", None)
        item.pop("after_json", None)
        item.pop("daily_memory_json", None)
        result.append(item)
    return result


async def delete_evolution_run(actor: str, run_id: str) -> dict:
    actor = _require_actor(actor)
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM persona_evolution_runs WHERE actor=? AND id=? LIMIT 1",
            (actor, run_id),
        )
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "deleted": False, "message": "记录不存在或已删除。"}
        await db.execute("DELETE FROM persona_evolution_items WHERE run_id=?", (run_id,))
        await db.execute(
            "DELETE FROM persona_evolution_runs WHERE actor=? AND id=?",
            (actor, run_id),
        )
        await db.commit()
    return {"ok": True, "deleted": True, "message": "记录已删除。"}


async def cleanup_old_main_ai_evolution_runs(max_age_seconds: int = EVOLUTION_RUN_RETENTION_SECONDS) -> int:
    return await cleanup_old_evolution_runs(ACTOR_MAIN_AI, max_age_seconds)


async def normalize_main_ai_evolution_fingerprints() -> int:
    return await normalize_evolution_fingerprints(ACTOR_MAIN_AI)


async def run_main_ai_persona_evolution(
    trigger: str = "manual",
    window_mode: str = "current",
    window_date: str | None = None,
) -> dict:
    return await run_persona_evolution(
        ACTOR_MAIN_AI,
        trigger=trigger,
        window_mode=window_mode,
        window_date=window_date,
    )


async def run_connor_persona_evolution(
    trigger: str = "manual",
    window_mode: str = "current",
    window_date: str | None = None,
) -> dict:
    return await run_persona_evolution(
        ACTOR_CONNOR,
        trigger=trigger,
        window_mode=window_mode,
        window_date=window_date,
    )


async def list_main_ai_evolution_runs(limit: int = 10) -> list[dict]:
    return await list_evolution_runs(ACTOR_MAIN_AI, limit)


async def list_connor_evolution_runs(limit: int = 10) -> list[dict]:
    return await list_evolution_runs(ACTOR_CONNOR, limit)


async def delete_main_ai_evolution_run(run_id: str) -> dict:
    return await delete_evolution_run(ACTOR_MAIN_AI, run_id)


async def delete_connor_evolution_run(run_id: str) -> dict:
    return await delete_evolution_run(ACTOR_CONNOR, run_id)


def _actor_auto_enabled(actor: str) -> bool:
    actor = _require_actor(actor)
    if actor == ACTOR_CONNOR:
        return bool(_chatroom_config().get("connor_persona_evolution_enabled"))
    return bool(load_worldbook().get("persona_evolution_enabled"))


async def persona_evolution_loop(actor: str):
    actor = _require_actor(actor)
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_5())
            if not _actor_auto_enabled(actor):
                print(f"[persona_evolution] {actor} disabled; skip scheduled run")
                continue
            result = await run_persona_evolution(actor, trigger="scheduled", window_mode="previous")
            print(f"[persona_evolution] {actor} {result.get('status')}: {result.get('message')}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[persona_evolution] {actor} scheduled error: {exc}")
            await asyncio.sleep(60)


async def main_ai_persona_evolution_loop():
    await persona_evolution_loop(ACTOR_MAIN_AI)


async def connor_persona_evolution_loop():
    await persona_evolution_loop(ACTOR_CONNOR)
