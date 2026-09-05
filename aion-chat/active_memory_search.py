"""主动记忆搜索：命令解析、时间过滤、角色隔离召回与上下文预算。"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Sequence

import aiosqlite

from database import get_db
from memory import _memory_time_payload, _unpack_embedding, cosine_similarity, get_embedding


MAX_REQUESTS = 5
MAX_QUERY_CHARS = 80
MAX_TOTAL_QUERY_CHARS = 300
MAX_RESULTS = 10
SUMMARY_LIMIT = 220
SOURCE_LIMIT = 400
MAX_SOURCES = 3
SUMMARY_SOFT_LIMIT = 2500
NORMAL_BLOCK_LIMIT = 4000
HARD_BLOCK_LIMIT = 8000

_COMMAND_RE = re.compile(
    r"[\[［【]\s*MEMORY_SEARCH\s*[:：]\s*(.*?)[\]］】]", re.I | re.S
)
_CSV_SPLIT_RE = re.compile(r"[,，、;；\n]+")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MemorySearchRequest:
    query: str
    mode: Literal["relevant", "latest", "earliest"] = "relevant"
    date_text: str = ""
    range_text: str = ""
    include_detail: bool = False


@dataclass
class MemorySearchResult:
    memory_id: str
    store: Literal["aion", "connor"]
    content: str
    occurred_at: float
    score: float
    hit_reasons: list[str]
    direct: bool
    sources: list[str] = field(default_factory=list)
    occurred_end: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)


def _clean_query(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[:MAX_QUERY_CHARS]


def extract_memory_search_requests(
    text: str, enabled: bool = True
) -> tuple[str, list[MemorySearchRequest]]:
    if not enabled:
        return text, []
    requests: list[MemorySearchRequest] = []
    used_chars = 0
    for match in _COMMAND_RE.finditer(text or ""):
        if len(requests) >= MAX_REQUESTS:
            break
        parts = [part.strip() for part in match.group(1).split("|")]
        query = _clean_query(parts[0] if parts else "")
        if not query or used_chars + len(query) > MAX_TOTAL_QUERY_CHARS:
            continue
        mode: Literal["relevant", "latest", "earliest"] = "relevant"
        date_text = ""
        range_text = ""
        include_detail = False
        for option in parts[1:]:
            lowered = option.lower()
            if lowered in {"relevant", "latest", "earliest"}:
                mode = lowered  # type: ignore[assignment]
            elif lowered == "detail":
                include_detail = True
            elif lowered.startswith("date="):
                date_text = option.split("=", 1)[1].strip()
            elif lowered.startswith("range="):
                range_text = option.split("=", 1)[1].strip()
        requests.append(MemorySearchRequest(query, mode, date_text, range_text, include_detail))
        used_chars += len(query)
    clean = _COMMAND_RE.sub("", text or "")
    return clean, requests


def parse_memory_keywords(raw) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            values = parsed if isinstance(parsed, list) else _CSV_SPLIT_RE.split(text)
        except (json.JSONDecodeError, TypeError):
            values = _CSV_SPLIT_RE.split(text)
    return [str(value).strip() for value in values if str(value).strip()]


def _logical_day_start(now: datetime) -> datetime:
    local = now.astimezone()
    logical_date = (local - timedelta(hours=5)).date()
    return datetime.combine(logical_date, datetime.min.time(), tzinfo=local.tzinfo) + timedelta(hours=5)


def _parse_date(value: str, anchor: datetime) -> datetime | None:
    value = str(value or "").strip()
    logical_today = _logical_day_start(anchor)
    offsets = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2}
    if value in offsets:
        return logical_today + timedelta(days=offsets[value])
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=anchor.astimezone().tzinfo, hour=5)
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日?", value)
    if match:
        return datetime(
            anchor.year, int(match.group(1)), int(match.group(2)), 5,
            tzinfo=anchor.astimezone().tzinfo,
        )
    return None


def resolve_memory_time_window(
    request: MemorySearchRequest, now: datetime | None = None
) -> tuple[float | None, float | None]:
    anchor = (now or datetime.now().astimezone()).astimezone()
    if request.range_text:
        parts = re.split(r"\.\.|至|到", request.range_text, maxsplit=1)
        if len(parts) == 2:
            start = _parse_date(parts[0], anchor)
            end_day = _parse_date(parts[1], anchor)
            if start and end_day:
                return start.timestamp(), (end_day + timedelta(days=1)).timestamp()
    if request.date_text:
        start = _parse_date(request.date_text, anchor)
        if start:
            return start.timestamp(), (start + timedelta(days=1)).timestamp()
    return None, None


def _norm(value: str) -> str:
    return _SPACE_RE.sub("", str(value or "")).casefold()


def rank_memory_rows(
    rows: Sequence[dict],
    requests: Sequence[MemorySearchRequest],
    *,
    actor: Literal["aion", "connor"],
    vector_scores: dict[tuple[str, int], float] | None = None,
    now: datetime | None = None,
) -> list[MemorySearchResult]:
    vector_scores = vector_scores or {}
    documents = []
    keyword_df: Counter[str] = Counter()
    for raw in rows:
        row = dict(raw)
        kws = parse_memory_keywords(row.get("keywords"))
        documents.append((row, kws))
        keyword_df.update(set(_norm(item) for item in kws if _norm(item)))
    total_docs = max(1, len(documents))
    merged: dict[str, MemorySearchResult] = {}
    modes = {request.mode for request in requests}

    for row, keywords in documents:
        time_info = _memory_time_payload(row)
        occurred = float(time_info.get("memory_time") or row.get("created_at") or 0)
        occurred_end = float(time_info.get("memory_time_end") or occurred)
        best_score = -1.0
        reasons: list[str] = []
        direct = False
        date_matched = False
        for request_index, request in enumerate(requests):
            window_start, window_end = resolve_memory_time_window(request, now=now)
            if window_start is not None and window_end is not None:
                if occurred_end < window_start or occurred >= window_end:
                    continue
                date_matched = True
            query = _norm(request.query)
            if not query:
                continue
            score = 0.0
            request_reasons: list[str] = []
            for keyword in keywords:
                normalized_keyword = _norm(keyword)
                if not normalized_keyword:
                    continue
                if query == normalized_keyword or query in normalized_keyword or normalized_keyword in query:
                    rarity = math.log((total_docs + 1) / (keyword_df[normalized_keyword] + 1)) + 1
                    exact_bonus = 1.5 if query == normalized_keyword else 1.0
                    score = max(score, 2.2 * rarity * exact_bonus)
                    request_reasons = [f"关键词精确命中：{keyword}"]
                    direct = True
            content = _norm(row.get("content"))
            if query and query in content:
                score = max(score, 1.8 + min(1.0, 8 / max(8, len(query))))
                request_reasons.append("正文精确命中")
                direct = True
            vec = max(0.0, float(vector_scores.get((str(row.get("id")), request_index), 0.0)))
            if vec:
                score += vec * 0.8
                if vec >= 0.45:
                    request_reasons.append("语义相似")
            if date_matched:
                score += 0.25
                request_reasons.append("时间窗口命中")
            score += min(0.08, max(0.0, float(row.get("importance") or 0.5)) * 0.05)
            if score > best_score:
                best_score = score
                reasons = request_reasons
        reliable = direct or best_score >= 0.38 or date_matched
        if not reliable:
            continue
        result = MemorySearchResult(
            memory_id=str(row.get("id")), store=actor, content=str(row.get("content") or ""),
            occurred_at=occurred, occurred_end=occurred_end, score=best_score,
            hit_reasons=list(dict.fromkeys(reasons)), direct=direct, raw=row,
        )
        previous = merged.get(result.memory_id)
        if previous is None or result.score > previous.score:
            merged[result.memory_id] = result

    results = list(merged.values())
    if "latest" in modes and "earliest" not in modes:
        results.sort(key=lambda item: (item.occurred_at, item.score), reverse=True)
    elif "earliest" in modes and "latest" not in modes:
        results.sort(key=lambda item: (item.occurred_at, -item.score))
    else:
        results.sort(key=lambda item: (item.score, item.occurred_at), reverse=True)
    return results[:MAX_RESULTS]


def actor_memory_query(actor: Literal["aion", "connor"]) -> tuple[str, tuple]:
    """Return the fixed SQL source for a backend-selected speaking actor."""
    fields = (
        "id, content, keywords, importance, embedding, created_at, source_start_ts, "
        "source_end_ts, source_msg_id"
    )
    if actor == "aion":
        return (
            f"SELECT {fields}, source_conv, evidence_summary FROM memories "
            "WHERE COALESCE(archive_state,'active')='active'",
            (),
        )
    if actor == "connor":
        return (
            f"SELECT {fields}, room_id, evidence_summary FROM chatroom_memories "
            "WHERE scope=? AND COALESCE(archive_state,'active')='active'",
            ("connor",),
        )
    raise ValueError("unsupported memory actor")


async def search_actor_memories(
    actor: Literal["aion", "connor"],
    requests: Sequence[MemorySearchRequest],
    now: datetime | None = None,
) -> list[MemorySearchResult]:
    if actor not in {"aion", "connor"}:
        raise ValueError("unsupported memory actor")
    limited = list(requests[:MAX_REQUESTS])
    if not limited:
        return []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        sql, params = actor_memory_query(actor)
        cursor = await db.execute(sql, params)
        rows = [dict(row) for row in await cursor.fetchall()]

    embeddings = await asyncio.gather(
        *(get_embedding(request.query) for request in limited), return_exceptions=True
    )
    vector_scores: dict[tuple[str, int], float] = {}
    for row in rows:
        blob = row.get("embedding")
        if not blob:
            continue
        try:
            memory_vector = _unpack_embedding(blob)
        except Exception:
            continue
        for index, query_vector in enumerate(embeddings):
            if isinstance(query_vector, list) and query_vector:
                vector_scores[(str(row.get("id")), index)] = cosine_similarity(query_vector, memory_vector)

    results = rank_memory_rows(rows, limited, actor=actor, vector_scores=vector_scores, now=now)
    if any(request.include_detail for request in limited) or (results and not results[0].direct):
        await _attach_source_details(results[:3], actor, limited)
    return results


async def _attach_source_details(
    results: Sequence[MemorySearchResult], actor: Literal["aion", "connor"], requests: Sequence[MemorySearchRequest]
) -> None:
    keywords = [request.query for request in requests]
    for result in results:
        try:
            if actor == "aion":
                from memory import fetch_source_details
                detail = await fetch_source_details([result.raw], keywords)
            else:
                from chatroom import fetch_chatroom_source_details
                detail = await fetch_chatroom_source_details([result.raw], keywords)
        except Exception:
            continue
        lines = [line.strip() for line in str(detail or "").splitlines() if line.strip()]
        result.sources = [line[:SOURCE_LIMIT] for line in lines[-MAX_SOURCES:]]


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "时间未知"


def format_memory_search_context(
    results: Sequence[MemorySearchResult], original_question: str
) -> str:
    header = (
        "[主动记忆搜索回执]\n"
        f"原始问题：{_SPACE_RE.sub(' ', original_question or '').strip()[:500]}\n"
        "请区分实际发生、计划/讨论、事后反应和同一事件的重复摘要；有冲突就说明不确定。\n"
    )
    if not results:
        return (header + "没有找到可靠记忆。")[:HARD_BLOCK_LIMIT]
    summary_lines: list[str] = []
    for index, result in enumerate(results[:MAX_RESULTS], 1):
        label = "直接事件候选" if result.direct else "关联背景"
        content = _SPACE_RE.sub(" ", result.content).strip()[:SUMMARY_LIMIT]
        reasons = "、".join(result.hit_reasons[:3]) or "语义相关"
        line = f"{index}. [{label}｜{_format_time(result.occurred_at)}｜{reasons}] {content}"
        if len(header) + sum(len(item) + 1 for item in summary_lines) + len(line) > SUMMARY_SOFT_LIMIT:
            remaining = SUMMARY_SOFT_LIMIT - len(header) - sum(len(item) + 1 for item in summary_lines)
            line = line[:max(0, remaining)]
        summary_lines.append(line)
    block = header + "\n".join(summary_lines)
    detail_lines: list[str] = []
    for index, result in enumerate(results[:MAX_RESULTS], 1):
        for source in result.sources[:MAX_SOURCES]:
            line = f"\n- 候选 {index} 来源原文：{_SPACE_RE.sub(' ', source).strip()[:SOURCE_LIMIT]}"
            if len(block) + sum(map(len, detail_lines)) + len(line) > NORMAL_BLOCK_LIMIT:
                break
            detail_lines.append(line)
    return (block + "".join(detail_lines))[:HARD_BLOCK_LIMIT]
