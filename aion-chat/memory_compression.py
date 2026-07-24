"""Calendar-based, traceable memory compression.

Models receive period-grouped memory text only. Database identifiers and
archive transitions remain deterministic application responsibilities.
"""

from __future__ import annotations

import json
import asyncio
import re
import time
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Iterable

import aiosqlite

from ai_providers import simple_ai_call
from config import MODELS, is_model_deprecated
from database import get_db


DAY_START_HOUR = 5
LEVELS = {
    "daily": {
        "source_stage": 0,
        "source_period_kind": "",
        "output_stage": 1,
        "output_period_kind": "day",
        "periods_per_call": 7,
        "title": "每日记忆",
    },
    "weekly": {
        "source_stage": 1,
        "source_period_kind": "day",
        "output_stage": 2,
        "output_period_kind": "week",
        "periods_per_call": 8,
        "title": "每周记忆",
    },
    "monthly": {
        "source_stage": 2,
        "source_period_kind": "week",
        "output_stage": 3,
        "output_period_kind": "month",
        "periods_per_call": 6,
        "title": "每月记忆",
    },
}
_ACTIVE_JOB_TASKS: dict[str, asyncio.Task] = {}


def _shifted_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts)) - timedelta(hours=DAY_START_HOUR)


def memory_day_for_ts(ts: float) -> str:
    return _shifted_datetime(ts).strftime("%Y-%m-%d")


def _day_bounds(day_text: str) -> tuple[float, float]:
    start = datetime.strptime(day_text, "%Y-%m-%d") + timedelta(hours=DAY_START_HOUR)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _legacy_capsule_day(row: dict) -> str:
    match = re.match(r"\s*(20\d{2}-\d{2}-\d{2})", str(row.get("content") or ""))
    if match:
        try:
            datetime.strptime(match.group(1), "%Y-%m-%d")
            return match.group(1)
        except ValueError:
            pass
    timestamp = float(row.get("created_at") or row.get("source_start_ts") or row.get("source_end_ts") or 0)
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


async def migrate_legacy_daily_capsules() -> dict:
    """Register old progressive-compression daily rows as first-stage day capsules."""
    counts = {"main": 0, "chatroom": 0}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, created_at, source_start_ts, source_end_ts "
            "FROM memories WHERE LOWER(type) IN ('daily','digest','seeky_digest','seeky_compressed') "
            "AND COALESCE(archive_state,'active')='active' "
            "AND COALESCE(compression_stage,0) IN (1,2) "
            "AND COALESCE(period_kind,'')=''"
        )
        main_rows = [dict(row) for row in await cur.fetchall()]
        for row in main_rows:
            start, end = _day_bounds(_legacy_capsule_day(row))
            cur = await db.execute(
                "UPDATE memories SET compression_stage=1, period_kind='day', "
                "period_start_ts=?, period_end_ts=? "
                "WHERE id=? AND COALESCE(period_kind,'')=''",
                (start, end, row["id"]),
            )
            counts["main"] += max(0, int(cur.rowcount or 0))

        cur = await db.execute(
            "SELECT id, content, created_at, source_start_ts, source_end_ts "
            "FROM chatroom_memories WHERE memory_kind='daily' "
            "AND COALESCE(archive_state,'active')='active' "
            "AND COALESCE(compression_stage,0) IN (1,2) "
            "AND COALESCE(period_kind,'')=''"
        )
        chatroom_rows = [dict(row) for row in await cur.fetchall()]
        for row in chatroom_rows:
            start, end = _day_bounds(_legacy_capsule_day(row))
            cur = await db.execute(
                "UPDATE chatroom_memories SET compression_stage=1, period_kind='day', "
                "period_start_ts=?, period_end_ts=? "
                "WHERE id=? AND COALESCE(period_kind,'')=''",
                (start, end, row["id"]),
            )
            counts["chatroom"] += max(0, int(cur.rowcount or 0))
        await db.commit()
    return counts


def _week_bounds(ts: float) -> tuple[str, float, float]:
    shifted = _shifted_datetime(ts)
    monday = shifted.date() - timedelta(days=shifted.weekday())
    start = datetime.combine(monday, datetime.min.time()) + timedelta(hours=DAY_START_HOUR)
    end = start + timedelta(days=7)
    label = f"{start.strftime('%Y-%m-%d')} ~ {(end - timedelta(days=1)).strftime('%Y-%m-%d')}"
    return label, start.timestamp(), end.timestamp()


def _month_bounds(ts: float) -> tuple[str, float, float]:
    shifted = _shifted_datetime(ts)
    start = datetime(shifted.year, shifted.month, 1, DAY_START_HOUR)
    if shifted.month == 12:
        end = datetime(shifted.year + 1, 1, 1, DAY_START_HOUR)
    else:
        end = datetime(shifted.year, shifted.month + 1, 1, DAY_START_HOUR)
    return start.strftime("%Y-%m"), start.timestamp(), end.timestamp()


def _current_week_start(now_ts: float) -> float:
    return _week_bounds(now_ts)[1]


def _current_month_start(now_ts: float) -> float:
    shifted = _shifted_datetime(now_ts)
    return datetime(shifted.year, shifted.month, 1, DAY_START_HOUR).timestamp()


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


async def ensure_calendar_compression_schema() -> None:
    async with get_db() as db:
        for table in ("memories", "chatroom_memories"):
            for column, definition in (
                ("archive_state", "TEXT DEFAULT 'active'"),
                ("archived_at", "REAL"),
                ("period_kind", "TEXT DEFAULT ''"),
                ("period_start_ts", "REAL"),
                ("period_end_ts", "REAL"),
                ("compression_batch_id", "TEXT DEFAULT ''"),
            ):
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except Exception:
                    pass
        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_compression_batches ("
            "id TEXT PRIMARY KEY, target TEXT NOT NULL, level TEXT NOT NULL, "
            "model_key TEXT NOT NULL, status TEXT NOT NULL, period_start_ts REAL, "
            "period_end_ts REAL, input_count INTEGER DEFAULT 0, output_count INTEGER DEFAULT 0, "
            "error TEXT DEFAULT '', created_at REAL NOT NULL, completed_at REAL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_compression_batch_inputs ("
            "batch_id TEXT NOT NULL, store TEXT NOT NULL, memory_id TEXT NOT NULL, "
            "PRIMARY KEY (batch_id, store, memory_id))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_compression_batch_outputs ("
            "batch_id TEXT NOT NULL, store TEXT NOT NULL, memory_id TEXT NOT NULL, "
            "PRIMARY KEY (batch_id, store, memory_id))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_compression_inputs_memory "
            "ON memory_compression_batch_inputs(store, memory_id)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_compression_jobs ("
            "id TEXT PRIMARY KEY, target TEXT NOT NULL, level TEXT NOT NULL, "
            "model_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', "
            "progress_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}', "
            "error TEXT DEFAULT '', created_at REAL NOT NULL, started_at REAL, "
            "updated_at REAL NOT NULL, completed_at REAL)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_compression_jobs_target "
            "ON memory_compression_jobs(target, level, created_at DESC)"
        )
        await db.commit()


def _row_event_ts(row: dict) -> float:
    return float(row.get("period_start_ts") or row.get("source_end_ts") or row.get("source_start_ts") or row.get("created_at") or 0)


def _period_for_row(level: str, row: dict) -> tuple[str, float, float]:
    ts = _row_event_ts(row)
    if level == "daily":
        label = memory_day_for_ts(ts)
        start, end = _day_bounds(label)
        return label, start, end
    if level == "weekly":
        return _week_bounds(ts)
    return _month_bounds(ts)


def _eligible_period(level: str, period_end: float, now_ts: float) -> bool:
    if level == "daily":
        return period_end <= now_ts - (7 * 86400)
    if level == "weekly":
        return (
            period_end <= _current_week_start(now_ts)
            and period_end <= now_ts - (7 * 86400)
        )
    return period_end <= _current_month_start(now_ts)


async def _candidate_rows(target: str, level: str) -> list[dict]:
    cfg = LEVELS[level]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if target == "main":
            params: list = [cfg["source_stage"]]
            period_sql = ""
            if cfg["source_period_kind"]:
                period_sql = "AND period_kind=? "
                params.append(cfg["source_period_kind"])
            cur = await db.execute(
                "SELECT id, content, type, created_at, source_conv, keywords, importance, "
                "source_start_ts, source_end_ts, source_msg_id, compression_stage, "
                "period_kind, period_start_ts, period_end_ts "
                "FROM memories WHERE LOWER(type) IN ('daily','digest','seeky_digest','seeky_compressed') "
                "AND COALESCE(archive_state,'active')='active' "
                "AND COALESCE(compression_stage,0)=? "
                f"{period_sql}"
                "ORDER BY COALESCE(period_start_ts, source_start_ts, created_at) ASC",
                tuple(params),
            )
        else:
            params = [cfg["source_stage"]]
            period_sql = ""
            if cfg["source_period_kind"]:
                period_sql = "AND period_kind=? "
                params.append(cfg["source_period_kind"])
            cur = await db.execute(
                "SELECT id, room_id, scope, content, keywords, importance, created_at, "
                "source_start_ts, source_end_ts, source_msg_id, memory_kind, compression_stage, "
                "period_kind, period_start_ts, period_end_ts "
                "FROM chatroom_memories WHERE memory_kind='daily' "
                "AND COALESCE(archive_state,'active')='active' "
                "AND COALESCE(compression_stage,0)=? "
                f"{period_sql}"
                "ORDER BY COALESCE(period_start_ts, source_start_ts, created_at) ASC",
                tuple(params),
            )
        return [dict(row) for row in await cur.fetchall()]


async def compression_preview(
    target: str,
    level: str,
    *,
    now_ts: float | None = None,
) -> dict:
    target = target if target in {"main", "chatroom"} else "main"
    if level not in LEVELS:
        raise ValueError("Unsupported compression level.")
    await ensure_calendar_compression_schema()
    now_ts = float(now_ts or time.time())
    blocked_periods: set[str] = set()
    if level == "weekly":
        for row in await _candidate_rows(target, "daily"):
            blocked_periods.add(_period_for_row("weekly", row)[0])
    elif level == "monthly":
        for row in await _candidate_rows(target, "weekly"):
            blocked_periods.add(_period_for_row("monthly", row)[0])
    grouped: dict[str, dict] = {}
    for row in await _candidate_rows(target, level):
        label, start, end = _period_for_row(level, row)
        if not _eligible_period(level, end, now_ts) or label in blocked_periods:
            continue
        period = grouped.setdefault(
            label,
            {
                "label": label,
                "period_start_ts": start,
                "period_end_ts": end,
                "rows": [],
            },
        )
        period["rows"].append(row)
    periods = []
    for period in sorted(grouped.values(), key=lambda item: item["period_start_ts"]):
        periods.append(
            {
                "label": period["label"],
                "period_start_ts": period["period_start_ts"],
                "period_end_ts": period["period_end_ts"],
                "memory_count": len(period["rows"]),
                "memory_ids": [row["id"] for row in period["rows"]],
            }
        )
    size = LEVELS[level]["periods_per_call"]
    return {
        "target": target,
        "level": level,
        "title": LEVELS[level]["title"],
        "memory_count": sum(period["memory_count"] for period in periods),
        "period_count": len(periods),
        "estimated_calls": (len(periods) + size - 1) // size if periods else 0,
        "periods": periods,
        "can_run": bool(periods),
    }


def _period_prompt(level: str, periods: list[dict], rows_by_id: dict[str, dict]) -> str:
    period_payload = []
    for period in periods:
        period_payload.append(
            {
                "period": period["label"],
                "memories": [rows_by_id[mem_id]["content"] for mem_id in period["memory_ids"]],
            }
        )
    level_instruction = {
        "daily": "每个日期根据实际价值保留 1-5 条主题明确的每日记忆；平淡日期可以少于 1 条。",
        "weekly": "每个自然周根据实际价值保留 1-5 条主题明确的每周记忆。",
        "monthly": "每个自然月根据实际价值保留 1-5 条完整的月度记忆；不限制单条字数。",
    }[level]
    return (
        "你正在整理陪伴记忆。输入已经由程序按时间周期分组。\n"
        "只归纳内容，不要输出、猜测或引用任何数据库 ID，也不要制造新事实。\n"
        f"{level_instruction}\n"
        "每条只表达一个可独立召回的主题，但可以写得完整。旅行、健康、关系变化、"
        "承诺、重要人物、宠物和项目节点优先保留；重复流水账可以合并或省略。\n"
        "稳定偏好、健康安全、长期承诺等原子事实放入 durable_facts，不占普通记忆名额。\n"
        "严格输出 JSON："
        '{"periods":[{"period":"输入中的周期标签","memories":['
        '{"content":"完整记忆","keywords":["关键词"],"importance":0.5}]}],'
        '"durable_facts":[{"content":"长期事实","keywords":["关键词"],"importance":0.85}]}\n'
        f"输入：{json.dumps(period_payload, ensure_ascii=False)}"
    )


def _parse_json_response(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


async def _call_compression_model(model_key: str, prompt: str) -> dict | None:
    raw = await simple_ai_call([{"role": "user", "content": prompt}], model_key)
    return _parse_json_response(raw or "")


async def _embedding_for_content(content: str):
    from memory import get_embedding

    return await get_embedding(content)


def _clean_keywords(value) -> list[str]:
    return [str(item).strip() for item in _json_list(value) if str(item).strip()][:8]


def _normalize_outputs(parsed: dict, expected_periods: set[str]) -> tuple[dict[str, list[dict]], list[dict]] | None:
    normalized: dict[str, list[dict]] = {label: [] for label in expected_periods}
    seen_periods = set()
    for period in parsed.get("periods") or []:
        if not isinstance(period, dict):
            continue
        label = str(period.get("period") or "").strip()
        if label not in expected_periods or label in seen_periods:
            continue
        seen_periods.add(label)
        for item in (period.get("memories") or [])[:5]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            try:
                importance = max(0.0, min(0.7, float(item.get("importance", 0.4))))
            except Exception:
                importance = 0.4
            normalized[label].append(
                {
                    "content": content,
                    "keywords": _clean_keywords(item.get("keywords")),
                    "importance": importance,
                }
            )
    if seen_periods != expected_periods:
        return None
    durable = []
    for item in (parsed.get("durable_facts") or [])[:8]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        try:
            importance = max(0.8, min(1.0, float(item.get("importance", 0.85))))
        except Exception:
            importance = 0.85
        durable.append(
            {
                "content": content,
                "keywords": _clean_keywords(item.get("keywords")),
                "importance": importance,
            }
        )
    return normalized, durable


def _chunks(items: list[dict], size: int) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


async def resolve_source_message_ids(target: str, memory_id: str, *, max_nodes: int = 500) -> list[str]:
    """Follow compression batches back to the original summaries' source IDs."""
    target = target if target in {"main", "chatroom"} else "main"
    pending = [(target, str(memory_id))]
    visited = set()
    source_ids = []
    source_seen = set()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        while pending and len(visited) < max_nodes:
            store, current_id = pending.pop(0)
            node = (store, current_id)
            if node in visited:
                continue
            visited.add(node)
            table = "memories" if store == "main" else "chatroom_memories"
            cur = await db.execute(
                f"SELECT source_msg_id, compression_batch_id FROM {table} WHERE id=?",
                (current_id,),
            )
            row = await cur.fetchone()
            if not row:
                continue
            for source_id in _json_list(row["source_msg_id"]):
                text = str(source_id).strip()
                if text and text not in source_seen:
                    source_seen.add(text)
                    source_ids.append(text)
            batch_id = str(row["compression_batch_id"] or "").strip()
            if not batch_id:
                continue
            cur = await db.execute(
                "SELECT store, memory_id FROM memory_compression_batch_inputs "
                "WHERE batch_id=? ORDER BY memory_id",
                (batch_id,),
            )
            pending.extend(
                (str(input_row["store"] or store), str(input_row["memory_id"]))
                for input_row in await cur.fetchall()
            )
    return source_ids


async def _insert_output(
    db,
    *,
    target: str,
    item: dict,
    memory_id: str,
    batch_id: str,
    stage: int,
    period_kind: str,
    period_start_ts: float,
    period_end_ts: float,
    embedding,
    durable: bool = False,
    template_row: dict | None = None,
) -> None:
    from memory import _pack_embedding

    packed = _pack_embedding(embedding) if embedding else None
    now = time.time()
    if target == "main":
        await db.execute(
            "INSERT INTO memories ("
            "id, content, type, created_at, source_conv, embedding, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id, compression_stage, "
            "evidence_summary, evidence_detail_level, archive_state, period_kind, "
            "period_start_ts, period_end_ts, compression_batch_id"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                item["content"],
                "important" if durable else "daily",
                period_start_ts or now,
                "calendar_memory_compression",
                packed,
                json.dumps(item["keywords"], ensure_ascii=False),
                item["importance"],
                period_start_ts,
                period_end_ts,
                0,
                json.dumps([], ensure_ascii=False),
                0 if durable else stage,
                f"由{period_kind}压缩批次整理，可沿批次查看冷档案来源。",
                "summary",
                "active",
                "fact" if durable else period_kind,
                period_start_ts,
                period_end_ts,
                batch_id,
            ),
        )
    else:
        template_row = template_row or {}
        await db.execute(
            "INSERT INTO chatroom_memories ("
            "id, room_id, scope, content, keywords, importance, embedding, source_start_ts, "
            "source_end_ts, created_at, unresolved, source_msg_id, memory_kind, compression_stage, "
            "evidence_summary, evidence_detail_level, archive_state, period_kind, "
            "period_start_ts, period_end_ts, compression_batch_id"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                template_row.get("room_id") or "connor_unified",
                template_row.get("scope") or "connor",
                item["content"],
                ",".join(item["keywords"]),
                item["importance"],
                packed,
                period_start_ts,
                period_end_ts,
                period_start_ts or now,
                0,
                json.dumps([], ensure_ascii=False),
                "long_term" if durable else "daily",
                0 if durable else stage,
                f"由{period_kind}压缩批次整理，可沿批次查看冷档案来源。",
                "summary",
                "active",
                "fact" if durable else period_kind,
                period_start_ts,
                period_end_ts,
                batch_id,
            ),
        )


async def run_calendar_compression(
    target: str,
    level: str,
    model_key: str,
    *,
    now_ts: float | None = None,
    progress_callback=None,
) -> dict:
    target = target if target in {"main", "chatroom"} else "main"
    if level not in LEVELS:
        return {"ok": False, "reason": "invalid_level", "message": "不支持的压缩层级。"}
    if model_key not in MODELS or is_model_deprecated(model_key):
        return {"ok": False, "reason": "invalid_model", "message": "请选择有效的压缩模型。"}
    now_ts = float(now_ts or time.time())
    preview = await compression_preview(target, level, now_ts=now_ts)
    if not preview["can_run"]:
        return {
            "ok": False,
            "reason": "no_candidates",
            "message": "目前没有可用于本次压缩的记忆，未调用模型。",
            "preview": preview,
        }
    if progress_callback:
        await progress_callback({
            "completed_calls": 0,
            "total_calls": preview["estimated_calls"],
            "processed_inputs": 0,
            "total_inputs": preview["memory_count"],
            "created_outputs": 0,
            "message": "候选已锁定，准备调用模型",
        })

    rows = await _candidate_rows(target, level)
    by_id = {row["id"]: row for row in rows}
    cfg = LEVELS[level]
    total_inputs = total_outputs = total_calls = 0
    batch_ids = []
    for period_chunk in _chunks(preview["periods"], cfg["periods_per_call"]):
        prompt = _period_prompt(level, period_chunk, by_id)
        parsed = await _call_compression_model(model_key, prompt)
        normalized = _normalize_outputs(parsed or {}, {period["label"] for period in period_chunk})
        if normalized is None:
            return {
                "ok": False,
                "reason": "invalid_model_output",
                "message": "模型没有返回可用的周期记忆，旧记忆保持活跃。",
                "input_count": total_inputs,
                "output_count": total_outputs,
            }
        period_outputs, durable_facts = normalized
        prepared = []
        for period in period_chunk:
            for item in period_outputs.get(period["label"], []):
                prepared.append(
                    {
                        "item": item,
                        "period": period,
                        "durable": False,
                        "embedding": await _embedding_for_content(item["content"]),
                    }
                )
        overall_start = min(period["period_start_ts"] for period in period_chunk)
        overall_end = max(period["period_end_ts"] for period in period_chunk)
        for item in durable_facts:
            prepared.append(
                {
                    "item": item,
                    "period": {
                        "period_start_ts": overall_start,
                        "period_end_ts": overall_end,
                    },
                    "durable": True,
                    "embedding": await _embedding_for_content(item["content"]),
                }
            )

        batch_id = f"mcb_{time.time_ns()}"
        input_ids = [mem_id for period in period_chunk for mem_id in period["memory_ids"]]
        template_row = by_id[input_ids[0]] if input_ids else {}
        async with get_db() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO memory_compression_batches ("
                    "id, target, level, model_key, status, period_start_ts, period_end_ts, "
                    "input_count, output_count, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        target,
                        level,
                        model_key,
                        "applying",
                        overall_start,
                        overall_end,
                        len(input_ids),
                        len(prepared),
                        time.time(),
                    ),
                )
                for mem_id in input_ids:
                    await db.execute(
                        "INSERT INTO memory_compression_batch_inputs (batch_id, store, memory_id) "
                        "VALUES (?,?,?)",
                        (batch_id, target, mem_id),
                    )
                output_ids = []
                for index, output in enumerate(prepared):
                    mem_id = f"memc_{time.time_ns()}_{index}"
                    await _insert_output(
                        db,
                        target=target,
                        item=output["item"],
                        memory_id=mem_id,
                        batch_id=batch_id,
                        stage=cfg["output_stage"],
                        period_kind=cfg["output_period_kind"],
                        period_start_ts=output["period"]["period_start_ts"],
                        period_end_ts=output["period"]["period_end_ts"],
                        embedding=output["embedding"],
                        durable=output["durable"],
                        template_row=template_row,
                    )
                    await db.execute(
                        "INSERT INTO memory_compression_batch_outputs (batch_id, store, memory_id) "
                        "VALUES (?,?,?)",
                        (batch_id, target, mem_id),
                    )
                    output_ids.append(mem_id)
                placeholders = ",".join("?" for _ in input_ids)
                table = "memories" if target == "main" else "chatroom_memories"
                await db.execute(
                    f"UPDATE {table} SET archive_state='cold', archived_at=?, embedding=NULL "
                    f"WHERE id IN ({placeholders}) AND COALESCE(archive_state,'active')='active'",
                    (time.time(), *input_ids),
                )
                await db.execute(
                    "UPDATE memory_compression_batches SET status='completed', completed_at=? WHERE id=?",
                    (time.time(), batch_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        total_inputs += len(input_ids)
        total_outputs += len(prepared)
        total_calls += 1
        batch_ids.append(batch_id)
        if progress_callback:
            await progress_callback({
                "completed_calls": total_calls,
                "total_calls": preview["estimated_calls"],
                "processed_inputs": total_inputs,
                "total_inputs": preview["memory_count"],
                "created_outputs": total_outputs,
                "message": f"已完成 {total_calls}/{preview['estimated_calls']} 个模型批次",
            })

    return {
        "ok": True,
        "message": f"压缩完成：处理 {total_inputs} 条旧记忆，生成 {total_outputs} 条新记忆。",
        "target": target,
        "level": level,
        "model_key": model_key,
        "input_count": total_inputs,
        "output_count": total_outputs,
        "model_calls": total_calls,
        "batch_ids": batch_ids,
    }


def _decode_json_object(value) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_job(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    return {
        "id": data["id"],
        "target": data["target"],
        "level": data["level"],
        "model_key": data["model_key"],
        "status": data["status"],
        "progress": _decode_json_object(data.get("progress_json")),
        "result": _decode_json_object(data.get("result_json")),
        "error": data.get("error") or "",
        "created_at": data.get("created_at"),
        "started_at": data.get("started_at"),
        "updated_at": data.get("updated_at"),
        "completed_at": data.get("completed_at"),
    }


async def _update_job_progress(job_id: str, progress: dict) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE memory_compression_jobs SET progress_json=?, updated_at=? "
            "WHERE id=? AND status='running'",
            (json.dumps(progress, ensure_ascii=False), time.time(), job_id),
        )
        await db.commit()


async def _execute_compression_job(
    job_id: str,
    target: str,
    level: str,
    model_key: str,
) -> None:
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "UPDATE memory_compression_jobs SET status='running', started_at=?, updated_at=?, "
            "progress_json=? WHERE id=? AND status='queued'",
            (
                now,
                now,
                json.dumps({"message": "正在准备压缩任务"}, ensure_ascii=False),
                job_id,
            ),
        )
        await db.commit()
    try:
        result = await run_calendar_compression(
            target,
            level,
            model_key,
            progress_callback=lambda progress: _update_job_progress(job_id, progress),
        )
        finished = time.time()
        status = "completed" if result.get("ok") else "failed"
        error = "" if result.get("ok") else str(result.get("message") or "压缩失败")
        async with get_db() as db:
            await db.execute(
                "UPDATE memory_compression_jobs SET status=?, result_json=?, error=?, "
                "updated_at=?, completed_at=? WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    error,
                    finished,
                    finished,
                    job_id,
                ),
            )
            await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        finished = time.time()
        async with get_db() as db:
            await db.execute(
                "UPDATE memory_compression_jobs SET status='failed', error=?, updated_at=?, "
                "completed_at=? WHERE id=?",
                (str(exc), finished, finished, job_id),
            )
            await db.commit()


async def create_calendar_compression_job(
    target: str,
    level: str,
    model_key: str,
) -> dict:
    target = target if target in {"main", "chatroom"} else "main"
    if level not in LEVELS:
        return {"ok": False, "reason": "invalid_level", "message": "不支持的压缩层级。"}
    if model_key not in MODELS or is_model_deprecated(model_key):
        return {"ok": False, "reason": "invalid_model", "message": "请选择有效的压缩模型。"}
    preview = await compression_preview(target, level)
    if not preview["can_run"]:
        return {
            "ok": False,
            "reason": "no_candidates",
            "message": "目前没有可用于本次压缩的记忆，未创建任务，也未调用模型。",
            "preview": preview,
        }
    await ensure_calendar_compression_schema()
    job_id = f"mcj_{time.time_ns()}"
    now = time.time()
    initial_progress = {
        "completed_calls": 0,
        "total_calls": preview["estimated_calls"],
        "processed_inputs": 0,
        "total_inputs": preview["memory_count"],
        "created_outputs": 0,
        "message": "任务已排队",
    }
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT * FROM memory_compression_jobs "
            "WHERE target=? AND level=? AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1",
            (target, level),
        )
        existing = await cur.fetchone()
        if existing:
            await db.rollback()
            return {
                "ok": False,
                "reason": "already_running",
                "message": "这个压缩层级已有任务正在进行。",
                "job": _serialize_job(existing),
            }
        await db.execute(
            "INSERT INTO memory_compression_jobs ("
            "id, target, level, model_key, status, progress_json, result_json, "
            "created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                target,
                level,
                model_key,
                "queued",
                json.dumps(initial_progress, ensure_ascii=False),
                "{}",
                now,
                now,
            ),
        )
        await db.commit()
    task = asyncio.create_task(_execute_compression_job(job_id, target, level, model_key))
    _ACTIVE_JOB_TASKS[job_id] = task
    task.add_done_callback(lambda _task, current_id=job_id: _ACTIVE_JOB_TASKS.pop(current_id, None))
    return {
        "ok": True,
        "message": "压缩任务已开始，可离开页面后再回来查看。",
        "job": {
            "id": job_id,
            "target": target,
            "level": level,
            "model_key": model_key,
            "status": "queued",
            "progress": initial_progress,
            "result": {},
            "error": "",
            "created_at": now,
            "updated_at": now,
        },
    }


async def get_calendar_compression_job(job_id: str) -> dict | None:
    await ensure_calendar_compression_schema()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM memory_compression_jobs WHERE id=?",
            (job_id,),
        )
        row = await cur.fetchone()
    job = _serialize_job(row)
    if job and job["status"] in {"queued", "running"} and job_id not in _ACTIVE_JOB_TASKS:
        finished = time.time()
        async with get_db() as db:
            await db.execute(
                "UPDATE memory_compression_jobs SET status='failed', error=?, "
                "updated_at=?, completed_at=? WHERE id=? AND status IN ('queued','running')",
                ("服务曾重启或任务已中断，请重新发起压缩。", finished, finished, job_id),
            )
            await db.commit()
        return await get_calendar_compression_job(job_id)
    return job


async def list_latest_calendar_compression_jobs(target: str) -> dict[str, dict]:
    await ensure_calendar_compression_schema()
    target = target if target in {"main", "chatroom"} else "main"
    result = {}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for level in LEVELS:
            cur = await db.execute(
                "SELECT * FROM memory_compression_jobs WHERE target=? AND level=? "
                "ORDER BY created_at DESC LIMIT 1",
                (target, level),
            )
            row = await cur.fetchone()
            if row:
                result[level] = _serialize_job(row)
    for level, job in list(result.items()):
        if job["status"] in {"queued", "running"}:
            result[level] = await get_calendar_compression_job(job["id"])
    return result
