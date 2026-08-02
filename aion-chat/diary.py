"""
AI 日记：保存总结后的私密日记，并按模型决定可选发布朋友圈。
"""

import asyncio
import json
import time
from typing import Any, Optional

from database import get_db
from ws import manager


def _is_diary_payload(data: Any) -> bool:
    """只接受包含日记字段的顶层对象，避免误把嵌套的 moment 当成完整结果。"""
    return isinstance(data, dict) and "diary" in data


def _repair_unescaped_json_quotes(candidate: str, max_repairs: int = 32) -> Optional[dict[str, Any]]:
    """有限修复模型在 JSON 字符串正文中遗漏转义的双引号。"""
    repaired = candidate
    for _ in range(max_repairs + 1):
        try:
            data = json.loads(repaired, strict=False)
            return data if _is_diary_payload(data) else None
        except json.JSONDecodeError as exc:
            # 当字符串被正文里的裸引号提前截断时，解析器会在该引号之后报告缺少逗号。
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
                # 引用文字后紧跟英文逗号时，解析器会把内层引号和逗号误当作字段结束。
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


def parse_diary_payload(raw: str) -> Optional[dict[str, Any]]:
    """从模型输出中提取日记 JSON。"""
    if not raw:
        return None
    text = raw.strip()
    decoder = json.JSONDecoder(strict=False)
    # 兼容代码块、前后解释文字和多个候选片段；从每个左花括号尝试解析首个完整对象。
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if _is_diary_payload(data):
            return data

    # 常见模型漂移：日记正文引用原话时忘记把双引号写成 \"。
    # 仅修复可重新解析且结构完整的候选对象，其他格式错误继续按失败处理。
    final_brace = text.rfind("}")
    if final_brace >= 0:
        for start, char in enumerate(text[:final_brace]):
            if char != "{":
                continue
            data = _repair_unescaped_json_quotes(text[start:final_brace + 1])
            if data is not None:
                return data
    return None


_MODEL_ERROR_PREFIXES = (
    "[硅基流动错误",
    "[中转站错误",
    "[Gemini错误",
    "[AntigravityCLI错误",
    "[CodexCLI错误",
    "[错误]",
)


def diary_response_error(raw: str) -> str:
    """识别被模型适配层作为普通文本返回的线路错误。"""
    text = str(raw or "").strip()
    if not text:
        return "模型返回为空"
    if text.startswith(_MODEL_ERROR_PREFIXES):
        return text[:300]
    lowered = text.lower()
    if "authentication required" in lowered or "authentication timed out" in lowered:
        return "模型线路认证失败或超时"
    return ""


def normalize_diary_payload(data: dict[str, Any]) -> tuple[dict[str, str], Optional[dict[str, Any]]]:
    """兼容轻微格式漂移，输出日记字段和可选朋友圈字段。"""
    diary_raw = data.get("diary", {})
    if isinstance(diary_raw, str):
        diary = {"title": "", "content": diary_raw, "mood": ""}
    elif isinstance(diary_raw, dict):
        diary = {
            "title": str(diary_raw.get("title") or "").strip(),
            "content": str(diary_raw.get("content") or "").strip(),
            "mood": str(diary_raw.get("mood") or "").strip(),
        }
    else:
        diary = {"title": "", "content": "", "mood": ""}

    moment = None
    if bool(data.get("post_moment")):
        moment_raw = data.get("moment", {})
        if isinstance(moment_raw, str):
            moment = {"content": moment_raw.strip(), "expect_reply": False}
        elif isinstance(moment_raw, dict):
            moment = {
                "content": str(moment_raw.get("content") or "").strip(),
                "expect_reply": bool(moment_raw.get("expect_reply", False)),
            }

    return diary, moment


async def save_diary_entry(
    *,
    author: str,
    title: str,
    content: str,
    mood: str = "",
    source_type: str = "",
    source_ref: str = "",
    source_start_ts: float | None = None,
    source_end_ts: float | None = None,
) -> Optional[dict[str, Any]]:
    """保存一篇 AI 日记，并广播给打开的日记本页面。"""
    content = (content or "").strip()
    if not content:
        return None
    now = time.time()
    entry_id = f"di_{author}_{int(now * 1000)}"
    entry = {
        "id": entry_id,
        "author": author,
        "title": (title or "").strip(),
        "content": content,
        "mood": (mood or "").strip(),
        "source_type": source_type or "",
        "source_ref": source_ref or "",
        "source_start_ts": source_start_ts,
        "source_end_ts": source_end_ts,
        "created_at": now,
    }
    async with get_db() as db:
        await db.execute(
            "INSERT INTO diary_entries "
            "(id, author, title, content, mood, source_type, source_ref, source_start_ts, source_end_ts, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entry["id"], entry["author"], entry["title"], entry["content"],
                entry["mood"], entry["source_type"], entry["source_ref"],
                entry["source_start_ts"], entry["source_end_ts"], entry["created_at"],
            ),
        )
        await db.commit()
    await manager.broadcast({"type": "diary_new", "data": entry})
    return entry


async def publish_ai_moment(
    *,
    author: str,
    content: str,
    expect_reply: bool = False,
    source_conv: str | None = None,
    source_msg_id: str | None = None,
) -> Optional[dict[str, Any]]:
    """复用现有朋友圈数据结构发布 AI 朋友圈。"""
    content = (content or "").strip()
    if not content:
        return None
    now = time.time()
    moment_id = f"mt_{int(now * 1000)}"
    expect = 1 if expect_reply else 0
    async with get_db() as db:
        await db.execute(
            "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (moment_id, author, content, source_conv, source_msg_id, expect, now),
        )
        await db.commit()
    moment_data = {
        "id": moment_id,
        "author": author,
        "content": content,
        "source_conv": source_conv,
        "source_msg_id": source_msg_id,
        "expect_reply": expect,
        "created_at": now,
        "comments": [],
        "reactions": [],
    }
    await manager.broadcast({"type": "moment_new", "data": moment_data})
    if expect_reply:
        from routes.moments import _trigger_ai_replies
        asyncio.create_task(_trigger_ai_replies(moment_id, exclude_author=author))
    return moment_data
