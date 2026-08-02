"""
Heart-rate classification and prompt helpers for ring data.

This module intentionally stays model-free: it classifies incoming heart-rate
points, records low-cost server-side events, and builds compact summaries for
other prompt paths.
"""

import json
import time
from datetime import datetime
from typing import Optional

import aiosqlite

from database import get_db


DEFAULT_HEART_CONFIG = {
    "sleep_low_max": 65,
    "normal_min": 70,
    "normal_max": 95,
    "elevated_min": 96,
    "exercise_min": 100,
    "attention_low": 45,
    "attention_high": 135,
    "large_delta": 25,
    "night_start_hour": 0,
    "night_end_hour": 6,
    "stale_minutes": 30,
}

CONFIG_FIELDS = tuple(DEFAULT_HEART_CONFIG.keys())

CATEGORY_LABELS = {
    "attention_low": "关注过低",
    "sleep_low": "睡眠低位",
    "low": "偏低",
    "normal": "正常",
    "elevated": "偏高",
    "exercise": "运动高心率",
    "attention_high": "关注过高",
}

EVENT_LABELS = {
    "attention_low": "心率过低",
    "attention_high": "心率过高",
    "large_delta": "心率突变",
    "night_high": "夜间偏高",
    "sleep_candidate": "可能入睡",
    "wake_candidate": "可能醒来",
    "exercise_candidate": "可能运动",
}

EVENT_COOLDOWNS = {
    "attention_low": 30 * 60,
    "attention_high": 30 * 60,
    "large_delta": 30 * 60,
    "night_high": 60 * 60,
    "sleep_candidate": 90 * 60,
    "wake_candidate": 45 * 60,
    "exercise_candidate": 45 * 60,
}


def _row_dict(row):
    return dict(row) if row else None


async def _ensure_heart_config(db) -> dict:
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT * FROM health_heart_config WHERE id=1")
    row = _row_dict(await cur.fetchone())
    if row:
        return {**DEFAULT_HEART_CONFIG, **{k: int(row[k]) for k in CONFIG_FIELDS}, "updated_at": row.get("updated_at", 0)}

    now = time.time()
    await db.execute(
        """
        INSERT INTO health_heart_config (
            id, sleep_low_max, normal_min, normal_max, elevated_min, exercise_min,
            attention_low, attention_high, large_delta, night_start_hour,
            night_end_hour, stale_minutes, updated_at
        ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        tuple(DEFAULT_HEART_CONFIG[k] for k in CONFIG_FIELDS) + (now,),
    )
    return {**DEFAULT_HEART_CONFIG, "updated_at": now}


async def get_heart_config(db=None) -> dict:
    if db is not None:
        return await _ensure_heart_config(db)
    async with get_db() as own_db:
        cfg = await _ensure_heart_config(own_db)
        await own_db.commit()
        return cfg


def normalize_heart_config(values: dict, base: Optional[dict] = None) -> tuple[Optional[dict], str]:
    cfg = {**DEFAULT_HEART_CONFIG}
    if base:
        cfg.update({k: int(base[k]) for k in CONFIG_FIELDS if k in base and base[k] is not None})

    for field in CONFIG_FIELDS:
        if field not in values or values[field] is None:
            continue
        try:
            cfg[field] = int(values[field])
        except (TypeError, ValueError):
            return None, f"{field} 需要是整数"

    if not 30 <= cfg["attention_low"] <= 120:
        return None, "关注过低阈值需要在 30-120 之间"
    if not 35 <= cfg["sleep_low_max"] <= 140:
        return None, "睡眠低心率阈值需要在 35-140 之间"
    if not 40 <= cfg["normal_min"] <= 180 or not 40 <= cfg["normal_max"] <= 190:
        return None, "正常区间需要在 40-190 之间"
    if cfg["attention_low"] >= cfg["sleep_low_max"]:
        return None, "关注过低阈值需要低于睡眠低心率阈值"
    if cfg["sleep_low_max"] >= cfg["normal_min"]:
        return None, "睡眠低心率阈值需要低于正常下限"
    if cfg["normal_min"] > cfg["normal_max"]:
        return None, "正常下限不能高于正常上限"
    if cfg["elevated_min"] <= cfg["normal_max"]:
        return None, "偏高起点需要高于正常上限"
    if cfg["exercise_min"] < cfg["elevated_min"]:
        return None, "运动阈值不能低于偏高起点"
    if cfg["attention_high"] < cfg["exercise_min"]:
        return None, "关注过高阈值不能低于运动阈值"
    if not 5 <= cfg["large_delta"] <= 100:
        return None, "突变阈值需要在 5-100 bpm 之间"
    if not 0 <= cfg["night_start_hour"] <= 23 or not 0 <= cfg["night_end_hour"] <= 23:
        return None, "夜间起止小时需要在 0-23 之间"
    if not 10 <= cfg["stale_minutes"] <= 1440:
        return None, "数据过期分钟需要在 10-1440 之间"
    return cfg, ""


async def update_heart_config(values: dict) -> dict:
    async with get_db() as db:
        base = await _ensure_heart_config(db)
        cfg, error = normalize_heart_config(values, base)
        if error:
            return {"error": error}
        now = time.time()
        cfg["updated_at"] = now
        await db.execute(
            """
            INSERT INTO health_heart_config (
                id, sleep_low_max, normal_min, normal_max, elevated_min, exercise_min,
                attention_low, attention_high, large_delta, night_start_hour,
                night_end_hour, stale_minutes, updated_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                sleep_low_max=excluded.sleep_low_max,
                normal_min=excluded.normal_min,
                normal_max=excluded.normal_max,
                elevated_min=excluded.elevated_min,
                exercise_min=excluded.exercise_min,
                attention_low=excluded.attention_low,
                attention_high=excluded.attention_high,
                large_delta=excluded.large_delta,
                night_start_hour=excluded.night_start_hour,
                night_end_hour=excluded.night_end_hour,
                stale_minutes=excluded.stale_minutes,
                updated_at=excluded.updated_at
            """,
            tuple(cfg[k] for k in CONFIG_FIELDS) + (now,),
        )
        await db.commit()
        return cfg


def classify_heart_rate(heart_rate: int, cfg: dict) -> str:
    hr = int(heart_rate)
    if hr <= cfg["attention_low"]:
        return "attention_low"
    if hr >= cfg["attention_high"]:
        return "attention_high"
    if hr >= cfg["exercise_min"]:
        return "exercise"
    if hr >= cfg["elevated_min"] or hr > cfg["normal_max"]:
        return "elevated"
    if hr < cfg["normal_min"]:
        return "sleep_low" if hr <= cfg["sleep_low_max"] else "low"
    return "normal"


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category or "未知")


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type or "事件")


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _is_night(measured_at: float, cfg: dict) -> bool:
    hour = datetime.fromtimestamp(float(measured_at)).hour
    return _hour_in_window(hour, cfg["night_start_hour"], cfg["night_end_hour"])


def _is_morning_after_night(measured_at: float, cfg: dict) -> bool:
    hour = datetime.fromtimestamp(float(measured_at)).hour
    end = cfg["night_end_hour"]
    for offset in range(0, 5):
        if hour == (end + offset) % 24:
            return True
    return False


async def _count_sleep_low_records(db, start_ts: float, end_ts: float, cfg: dict) -> int:
    cur = await db.execute(
        """
        SELECT COUNT(1) AS cnt FROM health_ring_heart_rates
        WHERE measured_at >= ? AND measured_at <= ? AND heart_rate <= ?
        """,
        (start_ts, end_ts, cfg["sleep_low_max"]),
    )
    row = await cur.fetchone()
    return int(row["cnt"] if isinstance(row, aiosqlite.Row) else row[0])


async def _previous_heart_record(db, entry: dict):
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT id, heart_rate, measured_at, source
        FROM health_ring_heart_rates
        WHERE id != ? AND measured_at < ?
        ORDER BY measured_at DESC LIMIT 1
        """,
        (entry["id"], float(entry["measured_at"])),
    )
    return _row_dict(await cur.fetchone())


async def _event_allowed(db, event_type: str, now: float) -> bool:
    cooldown = EVENT_COOLDOWNS.get(event_type, 30 * 60)
    cur = await db.execute(
        "SELECT created_at FROM health_heart_events WHERE event_type=? ORDER BY created_at DESC LIMIT 1",
        (event_type,),
    )
    row = await cur.fetchone()
    if not row:
        return True
    last_created = float(row["created_at"] if isinstance(row, aiosqlite.Row) else row[0])
    return now - last_created >= cooldown


async def _add_heart_event(
    db,
    *,
    event_type: str,
    severity: str,
    heart_rate: int,
    previous_heart_rate: Optional[int],
    delta: Optional[int],
    category: str,
    previous_category: str,
    measured_at: float,
    summary: str,
    details: dict,
):
    now = time.time()
    if not await _event_allowed(db, event_type, now):
        return None
    event_id = f"hrev_{int(float(measured_at) * 1000)}_{event_type}"
    cur = await db.execute("SELECT id FROM health_heart_events WHERE id=?", (event_id,))
    if await cur.fetchone():
        return None
    event = {
        "id": event_id,
        "event_type": event_type,
        "severity": severity,
        "heart_rate": int(heart_rate),
        "previous_heart_rate": previous_heart_rate,
        "delta": delta,
        "category": category,
        "previous_category": previous_category,
        "measured_at": float(measured_at),
        "summary": summary,
        "details_json": json.dumps(details, ensure_ascii=False),
        "created_at": now,
    }
    await db.execute(
        """
        INSERT INTO health_heart_events (
            id, event_type, severity, heart_rate, previous_heart_rate, delta,
            category, previous_category, measured_at, summary, details_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event["id"],
            event["event_type"],
            event["severity"],
            event["heart_rate"],
            event["previous_heart_rate"],
            event["delta"],
            event["category"],
            event["previous_category"],
            event["measured_at"],
            event["summary"],
            event["details_json"],
            event["created_at"],
        ),
    )
    await db.execute(
        "DELETE FROM health_heart_events "
        "WHERE id NOT IN (SELECT id FROM health_heart_events ORDER BY created_at DESC LIMIT 200)"
    )
    return event


async def _update_heart_state(db, entry: dict, category: str, events: list[dict], cfg: dict):
    measured_at = float(entry["measured_at"])
    cur = await db.execute("SELECT * FROM health_heart_state WHERE id=1")
    state = _row_dict(await cur.fetchone())
    if state and state.get("last_measured_at") and measured_at < float(state["last_measured_at"]):
        return

    is_night = _is_night(measured_at, cfg)
    sleep_since = None
    if category == "sleep_low" and is_night:
        sleep_since = state.get("sleep_candidate_since") if state else None
        sleep_since = sleep_since or measured_at

    high_since = None
    if category in {"elevated", "exercise", "attention_high"}:
        high_since = state.get("high_candidate_since") if state else None
        high_since = high_since or measured_at

    last_event_type = events[-1]["event_type"] if events else (state.get("last_event_type") if state else "")
    last_event_at = events[-1]["created_at"] if events else (state.get("last_event_at") if state else None)
    now = time.time()
    await db.execute(
        """
        INSERT INTO health_heart_state (
            id, current_category, last_heart_rate, last_measured_at,
            sleep_candidate_since, high_candidate_since, last_event_type,
            last_event_at, updated_at
        ) VALUES (1,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            current_category=excluded.current_category,
            last_heart_rate=excluded.last_heart_rate,
            last_measured_at=excluded.last_measured_at,
            sleep_candidate_since=excluded.sleep_candidate_since,
            high_candidate_since=excluded.high_candidate_since,
            last_event_type=excluded.last_event_type,
            last_event_at=excluded.last_event_at,
            updated_at=excluded.updated_at
        """,
        (
            category,
            int(entry["heart_rate"]),
            measured_at,
            sleep_since,
            high_since,
            last_event_type or "",
            last_event_at,
            now,
        ),
    )


async def analyze_heart_rate_entry(db, entry: Optional[dict]) -> list[dict]:
    if not entry or not entry.get("is_new"):
        return []

    db.row_factory = aiosqlite.Row
    cfg = await get_heart_config(db)
    heart_rate = int(entry["heart_rate"])
    measured_at = float(entry["measured_at"])
    category = classify_heart_rate(heart_rate, cfg)
    previous = await _previous_heart_record(db, entry)
    previous_hr = int(previous["heart_rate"]) if previous else None
    previous_category = classify_heart_rate(previous_hr, cfg) if previous_hr is not None else ""
    delta = heart_rate - previous_hr if previous_hr is not None else None

    details = {
        "entry_id": entry.get("id"),
        "source": entry.get("source", ""),
        "config": {k: cfg[k] for k in CONFIG_FIELDS},
    }
    events: list[dict] = []

    async def add(event_type: str, severity: str, summary: str):
        event = await _add_heart_event(
            db,
            event_type=event_type,
            severity=severity,
            heart_rate=heart_rate,
            previous_heart_rate=previous_hr,
            delta=delta,
            category=category,
            previous_category=previous_category,
            measured_at=measured_at,
            summary=summary,
            details=details,
        )
        if event:
            events.append(event)

    if category == "attention_low":
        await add("attention_low", "critical", f"心率 {heart_rate} bpm，低于关注低值 {cfg['attention_low']}。")
    if category == "attention_high":
        await add("attention_high", "critical", f"心率 {heart_rate} bpm，高于关注高值 {cfg['attention_high']}。")
    if delta is not None and abs(delta) >= cfg["large_delta"]:
        sign = "+" if delta > 0 else ""
        await add("large_delta", "warn", f"心率较上次变化 {sign}{delta} bpm，超过突变阈值 {cfg['large_delta']}。")

    is_night = _is_night(measured_at, cfg)
    if is_night and category in {"elevated", "exercise", "attention_high"}:
        await add("night_high", "warn", f"夜间心率 {heart_rate} bpm 仍处于{category_label(category)}，先记录观察。")

    if is_night and category == "sleep_low":
        low_count = await _count_sleep_low_records(db, measured_at - 30 * 60, measured_at, cfg)
        if low_count >= 2:
            await add("sleep_candidate", "info", f"夜间连续低心率（最近30分钟 {low_count} 条），可能进入睡眠低位。")

    if (
        _is_morning_after_night(measured_at, cfg)
        and category in {"normal", "elevated", "exercise"}
        and delta is not None
        and delta >= max(10, cfg["large_delta"] // 2)
    ):
        low_count = await _count_sleep_low_records(db, measured_at - 6 * 3600, measured_at - 1, cfg)
        if low_count >= 2:
            await add("wake_candidate", "info", f"心率从夜间低位回升到 {heart_rate} bpm，可能正在醒来。")

    if category == "exercise" and previous_category != "exercise" and not is_night:
        await add("exercise_candidate", "info", f"心率进入运动区间（{heart_rate} bpm，阈值 {cfg['exercise_min']}）。")

    await _update_heart_state(db, entry, category, events, cfg)
    return events


async def get_heart_events(db=None, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    if db is None:
        async with get_db() as own_db:
            return await get_heart_events(own_db, limit)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT id, event_type, severity, heart_rate, previous_heart_rate, delta,
               category, previous_category, measured_at, summary, details_json, created_at
        FROM health_heart_events ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_recent_heart_rates(db, limit: int = 8) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at
        FROM health_ring_heart_rates ORDER BY measured_at DESC LIMIT ?
        """,
        (max(1, min(int(limit or 8), 20)),),
    )
    return [dict(r) for r in await cur.fetchall()]


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")


def _fmt_age(seconds: float) -> str:
    seconds = max(0, float(seconds))
    if seconds < 90:
        return "刚刚"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = int(minutes // 60)
    rest = minutes % 60
    return f"{hours}小时{rest}分钟前" if rest else f"{hours}小时前"


async def build_heart_rate_summary_for_prompt(limit: int = 8) -> str:
    try:
        async with get_db() as db:
            cfg = await get_heart_config(db)
            records = await get_recent_heart_rates(db, limit)
            events = await get_heart_events(db, 3)
    except Exception:
        return ""

    if not records:
        return ""

    latest = records[0]
    latest_hr = int(latest["heart_rate"])
    latest_at = float(latest["measured_at"])
    now = time.time()
    age_seconds = now - latest_at
    category = classify_heart_rate(latest_hr, cfg)
    stale = age_seconds > cfg["stale_minutes"] * 60

    if stale:
        return (
            f"最近心率数据：{latest_hr} bpm（{_fmt_time(latest_at)}，{_fmt_age(age_seconds)}）。\n"
            f"状态：数据已超过 {cfg['stale_minutes']} 分钟未更新，可能是戒指没电、摘下或手机未同步；不要用它判断睡眠/清醒/运动。"
        )

    chronological = list(reversed(records[: min(6, len(records))]))
    trend = " -> ".join(str(int(r["heart_rate"])) for r in chronological)
    trend_delta = int(records[0]["heart_rate"]) - int(chronological[0]["heart_rate"])
    sign = "+" if trend_delta > 0 else ""
    event_text = "无"
    fresh_events = [e for e in events if now - float(e["created_at"]) <= 6 * 3600]
    if fresh_events:
        event_text = "；".join(
            f"{event_label(e['event_type'])}：{e['summary']}" for e in fresh_events[:2]
        )

    return (
        f"最近心率：{latest_hr} bpm（{_fmt_time(latest_at)}，{_fmt_age(age_seconds)}，{category_label(category)}）。\n"
        f"趋势：{trend}（最近{len(chronological)}条，{sign}{trend_delta} bpm）。\n"
        f"当前阈值：睡眠低位≤{cfg['sleep_low_max']}，正常{cfg['normal_min']}-{cfg['normal_max']}，运动≥{cfg['exercise_min']}，突变≥{cfg['large_delta']}。\n"
        f"最近心率事件：{event_text}。\n"
        "使用规则：心率只是辅助信号，不是医学诊断；必须结合画面、设备活动、聊天上下文判断，且不能覆盖摄像头事实。"
    )


async def build_heart_rate_prompt_block(user_name: str = "用户") -> str:
    summary = await build_heart_rate_summary_for_prompt()
    title = f"{user_name}最近心率摘要（戒指数据，仅作辅助）"
    if not summary:
        summary = "（暂无可用心率数据）"
    return f"\n\n【{title}】\n{summary}"
