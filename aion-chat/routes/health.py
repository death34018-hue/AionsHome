"""
健康功能 API：戒指最新快照、体重记录、姨妈期记录。
"""

import json
import re
import time
from datetime import date, datetime
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel

from database import get_db
from health_context import (
    analyze_heart_rate_entry,
    get_heart_config,
    get_heart_events,
    update_heart_config,
)
from ws import manager

router = APIRouter(prefix="/api/health", tags=["health"])

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ring_diag_info = {"info": "", "status": "", "ts": 0}


def _valid_date(value: str) -> bool:
    if not value or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _row_dict(row):
    return dict(row) if row else None


class RingSleep(BaseModel):
    start_at: Optional[float] = None
    end_at: Optional[float] = None
    total_min: Optional[int] = None
    deep_min: Optional[int] = None
    light_min: Optional[int] = None
    rem_min: Optional[int] = None
    wake_min: Optional[int] = None
    wake_count: Optional[int] = None


class RingSnapshot(BaseModel):
    device_name: str = ""
    heart_rate: Optional[int] = None
    systolic_bp: Optional[int] = None
    diastolic_bp: Optional[int] = None
    spo2: Optional[int] = None
    hrv: Optional[float] = None
    measured_at: Optional[float] = None
    sleep: Optional[RingSleep] = None
    raw: Optional[dict] = None


class RingHeartRate(BaseModel):
    device_name: str = ""
    heart_rate: int
    measured_at: Optional[float] = None
    source: str = ""
    raw: Optional[dict] = None


class HeartConfigUpdate(BaseModel):
    sleep_low_max: Optional[int] = None
    normal_min: Optional[int] = None
    normal_max: Optional[int] = None
    elevated_min: Optional[int] = None
    exercise_min: Optional[int] = None
    attention_low: Optional[int] = None
    attention_high: Optional[int] = None
    large_delta: Optional[int] = None
    night_start_hour: Optional[int] = None
    night_end_hour: Optional[int] = None
    stale_minutes: Optional[int] = None


class RingDiagReport(BaseModel):
    status: str = ""
    info: str = ""


class WeightEntry(BaseModel):
    date: str
    weight_kg: float
    note: str = ""


class PeriodEntry(BaseModel):
    id: str = ""
    start_date: str
    end_date: str = ""
    flow: str = ""
    symptoms: str = ""
    note: str = ""


class ManualStepsBody(BaseModel):
    date: str
    steps: int
    note: str = ""


class SleepEntryBody(BaseModel):
    date: str
    sleep_start_time: str = ""
    sleep_end_time: str = ""
    total_min: int = 0
    note: str = ""


def _valid_heart_rate(value: Optional[int]) -> bool:
    return isinstance(value, int) and 20 <= value <= 240


async def _insert_heart_rate(
    db,
    *,
    device_name: str,
    heart_rate: Optional[int],
    measured_at: Optional[float],
    source: str,
    raw: Optional[dict] = None,
):
    if not _valid_heart_rate(heart_rate):
        return None
    now = time.time()
    measured = measured_at or now
    entry_id = f"hr_{int(measured * 1000)}_{int(heart_rate)}"
    raw_json = json.dumps(raw or {}, ensure_ascii=False)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, created_at FROM health_ring_heart_rates WHERE id=?",
        (entry_id,),
    )
    existing = await cur.fetchone()
    is_new = existing is None
    await db.execute(
        """
        INSERT INTO health_ring_heart_rates
            (id, device_name, heart_rate, measured_at, source, raw_json, created_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            device_name=excluded.device_name,
            source=excluded.source,
            raw_json=excluded.raw_json
        """,
        (
            entry_id,
            (device_name or "").strip(),
            int(heart_rate),
            measured,
            (source or "").strip()[:40],
            raw_json,
            now,
        ),
    )
    await db.execute(
        "DELETE FROM health_ring_heart_rates "
        "WHERE id NOT IN (SELECT id FROM health_ring_heart_rates ORDER BY measured_at DESC LIMIT 20)"
    )
    return {
        "id": entry_id,
        "device_name": (device_name or "").strip(),
        "heart_rate": int(heart_rate),
        "measured_at": measured,
        "source": (source or "").strip()[:40],
        "raw_json": raw_json,
        "created_at": now if is_new else float(existing["created_at"] or now),
        "is_new": is_new,
    }


async def _recent_heart_rates(db, limit: int = 20):
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, device_name, heart_rate, measured_at, source, raw_json, created_at "
        "FROM health_ring_heart_rates ORDER BY measured_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


@router.get("/summary")
async def get_health_summary():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        ring = _row_dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_config = await get_heart_config(db)
        heart_events = await get_heart_events(db, 20)
        cur = await db.execute(
            "SELECT date, weight_kg, note, created_at, updated_at "
            "FROM health_weight_entries ORDER BY date DESC LIMIT 90"
        )
        weights = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries ORDER BY start_date DESC LIMIT 24"
        )
        periods = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT date, steps, note, created_at, updated_at "
            "FROM health_manual_steps ORDER BY date DESC LIMIT 90"
        )
        manual_steps = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT id, date, sleep_start_time, sleep_end_time, total_min, note, created_at, updated_at "
            "FROM health_sleep_entries ORDER BY date DESC LIMIT 90"
        )
        sleep_entries = [dict(r) for r in await cur.fetchall()]
    return {
        "ring": ring,
        "heartRates": heart_rates,
        "heartConfig": heart_config,
        "heartEvents": heart_events,
        "weights": weights,
        "periods": periods,
        "manualSteps": manual_steps,
        "sleepEntries": sleep_entries,
    }


@router.post("/ring/latest")
async def save_ring_latest(body: RingSnapshot):
    now = time.time()
    measured_at = body.measured_at or now
    sleep = body.sleep or RingSleep()
    raw_json = json.dumps(body.raw or {}, ensure_ascii=False)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_ring_latest (
                id, device_name, heart_rate, systolic_bp, diastolic_bp, spo2, hrv,
                measured_at, sleep_start_at, sleep_end_at, sleep_total_min,
                sleep_deep_min, sleep_light_min, sleep_rem_min, sleep_wake_min,
                sleep_wake_count, raw_json, synced_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                device_name=excluded.device_name,
                heart_rate=excluded.heart_rate,
                systolic_bp=excluded.systolic_bp,
                diastolic_bp=excluded.diastolic_bp,
                spo2=excluded.spo2,
                hrv=excluded.hrv,
                measured_at=excluded.measured_at,
                sleep_start_at=excluded.sleep_start_at,
                sleep_end_at=excluded.sleep_end_at,
                sleep_total_min=excluded.sleep_total_min,
                sleep_deep_min=excluded.sleep_deep_min,
                sleep_light_min=excluded.sleep_light_min,
                sleep_rem_min=excluded.sleep_rem_min,
                sleep_wake_min=excluded.sleep_wake_min,
                sleep_wake_count=excluded.sleep_wake_count,
                raw_json=excluded.raw_json,
                synced_at=excluded.synced_at
            """,
            (
                body.device_name.strip(),
                body.heart_rate,
                body.systolic_bp,
                body.diastolic_bp,
                body.spo2,
                body.hrv,
                measured_at,
                sleep.start_at,
                sleep.end_at,
                sleep.total_min,
                sleep.deep_min,
                sleep.light_min,
                sleep.rem_min,
                sleep.wake_min,
                sleep.wake_count,
                raw_json,
                now,
            ),
        )
        heart_entry = await _insert_heart_rate(
            db,
            device_name=body.device_name,
            heart_rate=body.heart_rate,
            measured_at=measured_at,
            source="snapshot",
            raw=body.raw,
        )
        heart_events_created = await analyze_heart_rate_entry(db, heart_entry)
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        row = dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_events = await get_heart_events(db, 20)
    await manager.broadcast({"type": "health_ring_updated", "data": row})
    if heart_entry:
        await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": heart_rates})
    if heart_events_created:
        await manager.broadcast({"type": "health_heart_events_updated", "items": heart_events})
        for event in heart_events_created:
            await manager.broadcast({"type": "health_heart_event_created", "data": event})
    return row


@router.get("/ring/heart-rates")
async def list_ring_heart_rates(limit: int = Query(20, ge=1, le=20)):
    async with get_db() as db:
        rows = await _recent_heart_rates(db, limit)
    return {"items": rows}


@router.post("/ring/request-diag")
async def request_ring_diag():
    await manager.broadcast({"type": "request_ring_diag"})
    return {"ok": True}


@router.post("/ring/diag-report")
async def ring_diag_report(body: RingDiagReport):
    _ring_diag_info["info"] = body.info
    _ring_diag_info["status"] = body.status
    _ring_diag_info["ts"] = time.time()
    print(f"[RingDiag] {body.status} {body.info}")
    await manager.broadcast({"type": "health_ring_diag", "data": _ring_diag_info})
    return {"ok": True}


@router.get("/ring/diag")
async def get_ring_diag():
    return _ring_diag_info


@router.get("/heart/config")
async def get_heart_config_route():
    return await get_heart_config()


@router.put("/heart/config")
async def update_heart_config_route(body: HeartConfigUpdate):
    result = await update_heart_config(body.dict(exclude_none=True))
    if "error" not in result:
        await manager.broadcast({"type": "health_heart_config_updated", "data": result})
    return result


@router.get("/heart/events")
async def list_heart_events(limit: int = Query(20, ge=1, le=100)):
    return {"items": await get_heart_events(limit=limit)}


@router.post("/ring/heart-rate")
async def save_ring_heart_rate(body: RingHeartRate):
    if not _valid_heart_rate(body.heart_rate):
        return {"error": "心率数值不正确"}
    now = time.time()
    measured_at = body.measured_at or now
    raw_json = json.dumps(body.raw or {}, ensure_ascii=False)
    async with get_db() as db:
        entry = await _insert_heart_rate(
            db,
            device_name=body.device_name,
            heart_rate=body.heart_rate,
            measured_at=measured_at,
            source=body.source or "realtime",
            raw=body.raw,
        )
        heart_events_created = await analyze_heart_rate_entry(db, entry)
        await db.execute(
            """
            INSERT INTO health_ring_latest (
                id, device_name, heart_rate, measured_at, raw_json, synced_at
            ) VALUES (1,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                device_name=CASE
                    WHEN excluded.device_name != '' THEN excluded.device_name
                    ELSE health_ring_latest.device_name
                END,
                heart_rate=excluded.heart_rate,
                measured_at=excluded.measured_at,
                raw_json=excluded.raw_json,
                synced_at=excluded.synced_at
            """,
            (
                (body.device_name or "").strip(),
                int(body.heart_rate),
                measured_at,
                raw_json,
                now,
            ),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM health_ring_latest WHERE id=1")
        ring = dict(await cur.fetchone())
        heart_rates = await _recent_heart_rates(db, 20)
        heart_events = await get_heart_events(db, 20)
    await manager.broadcast({"type": "health_ring_updated", "data": ring})
    await manager.broadcast({"type": "health_ring_heart_rates_updated", "items": heart_rates})
    if heart_events_created:
        await manager.broadcast({"type": "health_heart_events_updated", "items": heart_events})
        for event in heart_events_created:
            await manager.broadcast({"type": "health_heart_event_created", "data": event})
    return {"ring": ring, "entry": entry, "items": heart_rates, "events": heart_events_created, "heartEvents": heart_events}


@router.get("/weights")
async def list_weights(
    start: str = Query("", max_length=10),
    end: str = Query("", max_length=10),
    limit: int = Query(180, ge=1, le=1000),
):
    where = []
    params: list[object] = []
    if start and _valid_date(start):
        where.append("date >= ?")
        params.append(start)
    if end and _valid_date(end):
        where.append("date <= ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT date, weight_kg, note, created_at, updated_at "
            f"FROM health_weight_entries {where_sql} ORDER BY date DESC LIMIT ?",
            [*params, limit],
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/weights")
async def upsert_weight(body: WeightEntry):
    if not _valid_date(body.date):
        return {"error": "日期格式不正确"}
    if body.weight_kg <= 0 or body.weight_kg > 500:
        return {"error": "体重数值不正确"}
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_weight_entries (date, weight_kg, note, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                weight_kg=excluded.weight_kg,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (body.date, body.weight_kg, body.note.strip(), now, now),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT date, weight_kg, note, created_at, updated_at FROM health_weight_entries WHERE date=?",
            (body.date,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_weight_updated", "data": row})
    return row


@router.delete("/weights/{entry_date}")
async def delete_weight(entry_date: str):
    if not _valid_date(entry_date):
        return {"error": "日期格式不正确"}
    async with get_db() as db:
        await db.execute("DELETE FROM health_weight_entries WHERE date=?", (entry_date,))
        await db.commit()
    return {"ok": True}


@router.get("/periods")
async def list_periods(limit: int = Query(36, ge=1, le=200)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries ORDER BY start_date DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/periods")
async def upsert_period(body: PeriodEntry):
    if not _valid_date(body.start_date):
        return {"error": "开始日期格式不正确"}
    if body.end_date and not _valid_date(body.end_date):
        return {"error": "结束日期格式不正确"}
    if body.end_date and body.end_date < body.start_date:
        return {"error": "结束日期不能早于开始日期"}
    now = time.time()
    entry_id = body.id.strip() or f"hp_{int(now * 1000)}"
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_period_entries
                (id, start_date, end_date, flow, symptoms, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                flow=excluded.flow,
                symptoms=excluded.symptoms,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                entry_id,
                body.start_date,
                body.end_date,
                body.flow.strip(),
                body.symptoms.strip(),
                body.note.strip(),
                now,
                now,
            ),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, start_date, end_date, flow, symptoms, note, created_at, updated_at "
            "FROM health_period_entries WHERE id=?",
            (entry_id,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_period_updated", "data": row})
    return row


@router.delete("/periods/{entry_id}")
async def delete_period(entry_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM health_period_entries WHERE id=?", (entry_id,))
        await db.commit()
    return {"ok": True}


# ── 手动步数 ──

@router.get("/manual-steps")
async def list_manual_steps(
    start: str = Query("", max_length=10),
    end: str = Query("", max_length=10),
    limit: int = Query(180, ge=1, le=1000),
):
    where = []
    params: list[object] = []
    if start and _valid_date(start):
        where.append("date >= ?")
        params.append(start)
    if end and _valid_date(end):
        where.append("date <= ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT date, steps, note, created_at, updated_at "
            f"FROM health_manual_steps {where_sql} ORDER BY date DESC LIMIT ?",
            [*params, limit],
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/manual-steps")
async def upsert_manual_steps(body: ManualStepsBody):
    if not _valid_date(body.date):
        return {"error": "日期格式不正确"}
    if body.steps < 0 or body.steps > 200000:
        return {"error": "步数数值不正确"}
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO health_manual_steps (date, steps, note, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                steps=excluded.steps,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (body.date, body.steps, body.note.strip()[:80], now, now),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT date, steps, note, created_at, updated_at FROM health_manual_steps WHERE date=?",
            (body.date,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_manual_steps_updated", "data": row})
    return row


@router.delete("/manual-steps/{entry_date}")
async def delete_manual_steps(entry_date: str):
    if not _valid_date(entry_date):
        return {"error": "日期格式不正确"}
    async with get_db() as db:
        await db.execute("DELETE FROM health_manual_steps WHERE date=?", (entry_date,))
        await db.commit()
    return {"ok": True}


# ── 手动睡眠 ──

@router.get("/sleep-entries")
async def list_sleep_entries(
    start: str = Query("", max_length=10),
    end: str = Query("", max_length=10),
    limit: int = Query(180, ge=1, le=1000),
):
    where = []
    params: list[object] = []
    if start and _valid_date(start):
        where.append("date >= ?")
        params.append(start)
    if end and _valid_date(end):
        where.append("date <= ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT id, date, sleep_start_time, sleep_end_time, total_min, note, created_at, updated_at "
            f"FROM health_sleep_entries {where_sql} ORDER BY date DESC LIMIT ?",
            [*params, limit],
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.post("/sleep-entries")
async def upsert_sleep_entry(body: SleepEntryBody):
    if not _valid_date(body.date):
        return {"error": "日期格式不正确"}
    if body.total_min < 0 or body.total_min > 1440:
        return {"error": "睡眠时长不正确（0-1440分钟）"}
    now = time.time()
    entry_id = f"se_{int(now * 1000)}"
    async with get_db() as db:
        # 同一天只保留一条睡眠记录（按日期覆盖）
        cur = await db.execute(
            "SELECT id FROM health_sleep_entries WHERE date=? LIMIT 1",
            (body.date,),
        )
        existing = await cur.fetchone()
        if existing:
            entry_id = existing[0]
        await db.execute(
            """
            INSERT INTO health_sleep_entries
                (id, date, sleep_start_time, sleep_end_time, total_min, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                date=excluded.date,
                sleep_start_time=excluded.sleep_start_time,
                sleep_end_time=excluded.sleep_end_time,
                total_min=excluded.total_min,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                entry_id,
                body.date,
                body.sleep_start_time.strip()[:8],
                body.sleep_end_time.strip()[:8],
                body.total_min,
                body.note.strip()[:80],
                now,
                now,
            ),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, date, sleep_start_time, sleep_end_time, total_min, note, created_at, updated_at "
            "FROM health_sleep_entries WHERE id=?",
            (entry_id,),
        )
        row = dict(await cur.fetchone())
    await manager.broadcast({"type": "health_sleep_entry_updated", "data": row})
    return row


@router.delete("/sleep-entries/{entry_id}")
async def delete_sleep_entry(entry_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM health_sleep_entries WHERE id=?", (entry_id,))
        await db.commit()
    return {"ok": True}
