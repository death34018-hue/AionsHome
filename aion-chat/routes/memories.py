"""
Memory CRUD API, manual digest, source viewing, and source filtering.
"""

import json
import time
from datetime import datetime
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import load_digest_anchor, load_worldbook, save_digest_anchor
from database import get_db
from memory import (
    _pack_embedding, get_embedding, manual_digest, rebuild_embeddings,
    memory_kind_for_type, memory_kind_label, generate_daily_compression_draft,
    get_latest_daily_compression_review, apply_daily_compression_review,
    discard_daily_compression_review, update_daily_compression_review, _memory_time_payload,
)
from memory_compression import (
    compression_preview,
    create_calendar_compression_job,
    get_calendar_compression_job,
    list_latest_calendar_compression_jobs,
    resolve_source_message_ids,
    run_calendar_compression,
)
from ws import manager
from sync_events import append_sync_event, attach_sync_seq

router = APIRouter()


class MemoryCreate(BaseModel):
    content: str
    type: str = "event"


class MemoryUpdate(BaseModel):
    content: str
    type: Optional[str] = None
    keywords: Optional[str] = None
    importance: Optional[float] = None
    unresolved: Optional[int] = None
    evidence_summary: Optional[str] = None


class MemoryKindUpdate(BaseModel):
    memory_kind: str


class AnchorReset(BaseModel):
    date: str


class MemorySourceSelection(BaseModel):
    source_message_ids: list[str] = []


class DailyCompressionRequest(BaseModel):
    target: str = "main"
    days: int = 15


class DailyCompressionDraftUpdate(BaseModel):
    payload: dict


class CalendarCompressionRequest(BaseModel):
    target: str = "main"
    level: str
    model_key: str


def _json_list(value):
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


def _source_ids_for_memory(mem) -> list[str]:
    ids = []
    source_conv = mem["source_conv"] if "source_conv" in mem.keys() else ""
    for raw in _json_list(mem["source_msg_id"] if "source_msg_id" in mem.keys() else ""):
        source_id = str(raw).strip()
        if not source_id:
            continue
        if ":" not in source_id:
            prefix = "chatroom" if str(source_conv or "").startswith("chatroom:") else "private"
            source_id = f"{prefix}:{source_id}"
        ids.append(source_id)
    return ids


def _keyword_list(value) -> list[str]:
    result = []
    for item in _json_list(value):
        text = str(item).strip().lower()
        if len(text) >= 2:
            result.append(text)
    if isinstance(value, str) and not result:
        for part in value.replace("，", ",").split(","):
            text = part.strip().lower()
            if len(text) >= 2:
                result.append(text)
    return result


def _content_needles(content: str) -> list[str]:
    text = "".join(ch.lower() for ch in content if not ch.isspace())
    needles = set()
    for size in (6, 4):
        for start in range(0, max(0, len(text) - size + 1)):
            part = text[start:start + size]
            if len(part) == size and not part.isdigit():
                needles.add(part)
        if len(needles) >= 12:
            break
    return list(needles)[:24]


def _source_score(mem, row, keywords: list[str], needles: list[str]) -> float:
    text = (row.get("content") or "").lower()
    if not text:
        return 0.0
    score = 0.0
    for kw in keywords:
        if kw and kw in text:
            score += 4.0
    for needle in needles:
        if needle and needle in text:
            score += 1.0
    mem_text = (mem["content"] if "content" in mem.keys() else "").lower()
    if mem_text and text and (mem_text in text or text in mem_text):
        score += 3.0
    return score


def _chatroom_sender_name(sender: str, user_name: str, ai_name: str) -> str:
    try:
        from chatroom import get_chatroom_names
        _, _, companion_name = get_chatroom_names()
    except Exception:
        companion_name = "第二AI"
    return {"user": user_name, "aion": ai_name, "connor": companion_name}.get(sender, sender)


async def _fetch_source_rows_by_ids(source_ids: list[str], user_name: str, ai_name: str) -> list[dict]:
    rows = []
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
                        "role": row["role"],
                        "name": user_name if row["role"] == "user" else ai_name,
                        "content": row["content"],
                        "created_at": row["created_at"],
                        "source": "private",
                    })
            elif prefix == "chatroom":
                cur = await db.execute(
                    "SELECT sender, content, created_at FROM chatroom_messages WHERE id=? AND sender != 'system'",
                    (raw_id,),
                )
                row = await cur.fetchone()
                if row:
                    rows.append({
                        "id": f"chatroom:{raw_id}",
                        "role": "assistant" if row["sender"] == "aion" else "user",
                        "name": _chatroom_sender_name(row["sender"], user_name, ai_name),
                        "content": row["content"],
                        "created_at": row["created_at"],
                        "source": "chatroom",
                    })
    return rows


@router.get("/api/memories")
async def list_memories(
    limit: int = Query(50, ge=1, le=100),
    before: Optional[float] = Query(None),
    q: str = Query("", max_length=200),
    kind: str = "all",
):
    """Cursor-paginated memories; the former unbounded response was slow outdoors."""
    search = (q or "").strip().lower()
    kind = str(kind or "all").strip().lower()
    if kind not in {"all", "daily", "long_term"}:
        raise HTTPException(status_code=400, detail="无效的记忆分类")
    where = ["COALESCE(archive_state,'active')='active'"]
    params: list = []
    filtered_where = ["COALESCE(archive_state,'active')='active'"]
    filtered_params: list = []
    sort_expr = "COALESCE(source_end_ts, source_start_ts, created_at)"
    if kind == "daily":
        kind_sql = "LOWER(type) IN ('daily','digest','seeky_digest','seeky_compressed')"
        where.append(kind_sql)
        filtered_where.append(kind_sql)
    elif kind == "long_term":
        kind_sql = "LOWER(type) NOT IN ('daily','digest','seeky_digest','seeky_compressed')"
        where.append(kind_sql)
        filtered_where.append(kind_sql)
    if before is not None:
        where.append(f"{sort_expr} < ?")
        params.append(before)
    if search:
        search_sql = "(LOWER(content) LIKE ? OR LOWER(COALESCE(keywords,'')) LIKE ? OR LOWER(type) LIKE ?)"
        where.append(search_sql)
        filtered_where.append(search_sql)
        needle = f"%{search}%"
        params.extend([needle, needle, needle])
        filtered_params.extend([needle, needle, needle])
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN LOWER(type) IN ('daily','digest','seeky_digest','seeky_compressed') "
            "THEN 1 ELSE 0 END) AS daily, "
            "SUM(CASE WHEN LOWER(type) NOT IN ('daily','digest','seeky_digest','seeky_compressed') "
            "THEN 1 ELSE 0 END) AS long_term "
            "FROM memories WHERE COALESCE(archive_state,'active')='active'",
        )
        count_row = await cur.fetchone()
        total = int(count_row["total"] or 0)
        kind_totals = {
            "all": total,
            "daily": int(count_row["daily"] or 0),
            "long_term": int(count_row["long_term"] or 0),
        }
        filtered_total = total
        if search or kind != "all":
            cur = await db.execute(
                f"SELECT COUNT(*) AS total FROM memories WHERE {' AND '.join(filtered_where)}",
                tuple(filtered_params),
            )
            filtered_total = int((await cur.fetchone())["total"] or 0)
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id, evidence_summary, evidence_detail_level "
            f"FROM memories{where_sql} ORDER BY {sort_expr} DESC LIMIT ?",
            (*params, limit + 1),
        )
        rows = await cur.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        result = []
        for row in rows:
            item = dict(row)
            item["memory_kind"] = memory_kind_for_type(item.get("type"))
            item["memory_kind_label"] = memory_kind_label(item.get("type"))
            item.update(_memory_time_payload(item))
            explicit_source = bool(str(item.get("source_msg_id") or "").strip())
            source_ids = _source_ids_for_memory(item)
            if explicit_source:
                item["source_count"] = len(source_ids)
            elif item.get("source_start_ts") and item.get("source_end_ts"):
                cur = await db.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ?",
                    (item["source_start_ts"], item["source_end_ts"]),
                )
                private_count = (await cur.fetchone())[0]
                cur = await db.execute(
                    "SELECT COUNT(*) FROM chatroom_messages "
                    "WHERE sender != 'system' AND created_at >= ? AND created_at <= ?",
                    (item["source_start_ts"], item["source_end_ts"]),
                )
                chatroom_count = (await cur.fetchone())[0]
                item["source_count"] = private_count + chatroom_count
            else:
                item["source_count"] = 0
            result.append(item)
    next_cursor = None
    if result:
        last = result[-1]
        next_cursor = last.get("source_end_ts") or last.get("source_start_ts") or last.get("created_at")
    return {
        "items": result,
        "total": total,
        "kind_totals": kind_totals,
        "filtered_total": filtered_total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.post("/api/memories")
async def create_memory(body: MemoryCreate):
    vec = await get_embedding(body.content)
    mem_id = f"mem_{int(time.time() * 1000)}"
    now = time.time()
    mem = {
        "id": mem_id,
        "content": body.content,
        "type": body.type,
        "created_at": now,
        "keywords": "",
        "importance": 0.5,
        "source_start_ts": None,
        "source_end_ts": None,
        "source_msg_id": None,
        "evidence_summary": "",
        "evidence_detail_level": "summary",
        "memory_kind": memory_kind_for_type(body.type),
        "memory_kind_label": memory_kind_label(body.type),
    }
    mem.update(_memory_time_payload(mem))
    event = {"type": "memory_added", "data": mem}
    async with get_db() as db:
        await db.execute(
            "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, evidence_summary, evidence_detail_level) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, body.content, body.type, now, None, _pack_embedding(vec) if vec else None, "", 0.5, None, None, "", "summary"),
        )
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))
    return mem


@router.put("/api/memories/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdate):
    vec = await get_embedding(body.content)
    async with get_db() as db:
        fields = ["content=?", "embedding=?"]
        params = [body.content, _pack_embedding(vec) if vec else None]
        if body.type is not None:
            fields.append("type=?")
            params.append(body.type)
        if body.keywords is not None:
            fields.append("keywords=?")
            params.append(body.keywords)
        if body.importance is not None:
            fields.append("importance=?")
            params.append(body.importance)
        if body.unresolved is not None:
            fields.append("unresolved=?")
            params.append(1 if body.unresolved else 0)
        if body.evidence_summary is not None:
            fields.append("evidence_summary=?")
            params.append(str(body.evidence_summary or "").strip())
        params.append(mem_id)
        await db.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id=?", params)
        event_data = {"id": mem_id, **body.model_dump(exclude_none=True)}
        if body.type is not None:
            event_data["memory_kind"] = memory_kind_for_type(body.type)
            event_data["memory_kind_label"] = memory_kind_label(body.type)
        event = {"type": "memory_updated", "data": event_data}
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))
    return {"ok": True, **event_data}


@router.patch("/api/memories/{mem_id}/kind")
async def update_memory_kind(mem_id: str, body: MemoryKindUpdate):
    selected_kind = "daily" if body.memory_kind == "daily" else "long_term" if body.memory_kind == "long_term" else ""
    if not selected_kind:
        raise HTTPException(status_code=400, detail="memory_kind must be daily or long_term")
    new_type = "daily" if selected_kind == "daily" else "important"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM memories WHERE id=?", (mem_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        await db.execute("UPDATE memories SET type=? WHERE id=?", (new_type, mem_id))
        event_data = {
            "id": mem_id,
            "type": new_type,
            "memory_kind": selected_kind,
            "memory_kind_label": memory_kind_label(new_type),
        }
        event = {"type": "memory_updated", "data": event_data}
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))
    return {
        "ok": True,
        **event_data,
    }


@router.delete("/api/memories/{mem_id}")
async def delete_memory(mem_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        event = {"type": "memory_deleted", "data": {"id": mem_id}}
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))
    return {"ok": True}


@router.patch("/api/memories/{mem_id}/unresolved")
async def toggle_unresolved(mem_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT unresolved FROM memories WHERE id=?", (mem_id,))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "message": "Memory not found"}
        new_val = 0 if row["unresolved"] else 1
        await db.execute("UPDATE memories SET unresolved=? WHERE id=?", (new_val, mem_id))
        event = {"type": "memory_updated", "data": {"id": mem_id, "unresolved": new_val}}
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))
    return {"ok": True, "unresolved": new_val}


@router.get("/api/memories/by-conv/{conv_id}")
async def get_memories_by_conv(conv_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, source_msg_id FROM memories WHERE source_conv=? AND source_msg_id IS NOT NULL",
            (conv_id,),
        )
        rows = await cur.fetchall()
    return [{"mem_id": r["id"], "content": r["content"], "msg_id": r["source_msg_id"]} for r in rows]


@router.post("/api/memories/digest")
async def trigger_digest():
    return await manual_digest()


@router.post("/api/memories/compress-daily")
async def trigger_daily_compression(body: Optional[DailyCompressionRequest] = None):
    payload = body or DailyCompressionRequest()
    return await generate_daily_compression_draft(days=payload.days, target=payload.target)


@router.get("/api/memories/compress-daily/latest")
async def latest_daily_compression_review(target: str = "main"):
    review = await get_latest_daily_compression_review(target=target)
    return {"ok": True, "review": review}


@router.post("/api/memories/compress-daily/{review_id}/apply")
async def apply_daily_compression(review_id: str):
    result = await apply_daily_compression_review(review_id)
    await manager.broadcast({
        "type": "memory_collection_changed",
        "data": {"review_id": review_id, "reason": "compression"},
    })
    return result


@router.patch("/api/memories/compress-daily/{review_id}")
async def update_daily_compression(review_id: str, body: DailyCompressionDraftUpdate):
    return await update_daily_compression_review(review_id, body.payload)


@router.post("/api/memories/compress-daily/{review_id}/discard")
async def discard_daily_compression(review_id: str):
    return await discard_daily_compression_review(review_id)


@router.get("/api/memories/calendar-compression/preview")
async def preview_calendar_compression(target: str = "main", level: str = "daily"):
    try:
        preview = await compression_preview(target=target, level=level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "preview": preview}


@router.post("/api/memories/calendar-compression/run")
async def trigger_calendar_compression(body: CalendarCompressionRequest):
    result = await run_calendar_compression(
        target=body.target,
        level=body.level,
        model_key=body.model_key,
    )
    if result.get("ok"):
        await manager.broadcast({
            "type": "memory_collection_changed",
            "data": {
                "reason": "calendar_compression",
                "target": body.target,
                "level": body.level,
            },
        })
    return result


@router.get("/api/memories/calendar-compression/jobs")
async def latest_calendar_compression_jobs(target: str = "main"):
    return {
        "ok": True,
        "jobs": await list_latest_calendar_compression_jobs(target),
    }


@router.post("/api/memories/calendar-compression/jobs")
async def start_calendar_compression_job(body: CalendarCompressionRequest):
    return await create_calendar_compression_job(
        target=body.target,
        level=body.level,
        model_key=body.model_key,
    )


@router.get("/api/memories/calendar-compression/jobs/{job_id}")
async def calendar_compression_job_status(job_id: str):
    job = await get_calendar_compression_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compression job not found")
    return {"ok": True, "job": job}


@router.post("/api/memories/rebuild-embeddings")
async def trigger_rebuild_embeddings():
    return await rebuild_embeddings()


@router.get("/api/memories/digest/anchor")
async def get_anchor():
    ts = load_digest_anchor()
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else "Never"
    return {"ok": True, "anchor_ts": ts, "anchor_date": date_str}


@router.post("/api/memories/digest/anchor")
async def reset_anchor(body: AnchorReset):
    try:
        if len(body.date) <= 10:
            dt = datetime.strptime(body.date, "%Y-%m-%d")
        else:
            dt = datetime.strptime(body.date, "%Y-%m-%d %H:%M:%S")
        ts = dt.timestamp()
        save_digest_anchor(ts)
        return {"ok": True, "anchor_ts": ts, "anchor_date": dt.strftime("%Y-%m-%d %H:%M:%S")}
    except ValueError:
        return {"ok": False, "message": "Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS"}


@router.get("/api/memories/{mem_id}/source")
async def get_memory_source(mem_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, keywords, source_conv, source_start_ts, source_end_ts, "
            "source_msg_id, compression_batch_id "
            "FROM memories WHERE id=?",
            (mem_id,),
        )
        mem = await cur.fetchone()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")

    selected_ids = set(_source_ids_for_memory(mem))
    if not selected_ids and str(mem["compression_batch_id"] or "").strip():
        selected_ids.update(await resolve_source_message_ids("main", mem_id))
    if not selected_ids and (not mem["source_start_ts"] or not mem["source_end_ts"]):
        return {"ok": False, "message": "No source messages for this memory"}

    wb = load_worldbook()
    user_name = wb.get("user_name", "User")
    ai_name = wb.get("ai_name", "AI")

    all_msgs = []
    if mem["source_start_ts"] and mem["source_end_ts"]:
        start_ts, end_ts = mem["source_start_ts"], mem["source_end_ts"]
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (start_ts, end_ts),
            )
            for row in await cur.fetchall():
                all_msgs.append({
                    "id": f"private:{row['id']}",
                    "role": row["role"],
                    "name": user_name if row["role"] == "user" else ai_name,
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "source": "private",
                })

            cur = await db.execute(
                "SELECT id, sender, content, created_at FROM chatroom_messages "
                "WHERE created_at >= ? AND created_at <= ? AND sender != 'system' "
                "ORDER BY created_at ASC",
                (start_ts, end_ts),
            )
            for row in await cur.fetchall():
                all_msgs.append({
                    "id": f"chatroom:{row['id']}",
                    "role": "assistant" if row["sender"] == "aion" else "user",
                    "name": _chatroom_sender_name(row["sender"], user_name, ai_name),
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "source": "chatroom",
                })

    if selected_ids:
        existing = {m["id"] for m in all_msgs}
        extra = await _fetch_source_rows_by_ids(list(selected_ids - existing), user_name, ai_name)
        all_msgs.extend([m for m in extra if m["id"] not in existing])
        all_msgs = [m for m in all_msgs if m["id"] in selected_ids]

    exact_mode = bool(selected_ids)
    keywords = _keyword_list(mem["keywords"])
    needles = _content_needles(mem["content"])
    scored = []
    for msg in all_msgs:
        score = 1.0 if msg["id"] in selected_ids else _source_score(mem, msg, keywords, needles)
        scored.append((score, msg["id"]))

    recommended_ids = selected_ids
    if not exact_mode and scored:
        positive = [(score, source_id) for score, source_id in scored if score > 0]
        positive.sort(key=lambda item: item[0], reverse=True)
        recommended_ids = {source_id for _, source_id in positive[:8]}

    all_msgs.sort(key=lambda x: x["created_at"])
    for msg in all_msgs:
        msg["selected"] = msg["id"] in recommended_ids
        msg["recommended"] = msg["selected"]

    return {
        "ok": True,
        "messages": all_msgs,
        "selected_count": sum(1 for msg in all_msgs if msg["selected"]),
        "selection_mode": "saved" if exact_mode else "suggested",
    }


@router.post("/api/memories/{mem_id}/source-selection")
async def save_memory_source_selection(mem_id: str, body: MemorySourceSelection):
    wb = load_worldbook()
    user_name = wb.get("user_name", "User")
    ai_name = wb.get("ai_name", "AI")

    source_ids = []
    seen = set()
    for source_id in body.source_message_ids:
        text = str(source_id).strip()
        if not text or ":" not in text or text in seen:
            continue
        prefix, raw_id = text.split(":", 1)
        if prefix not in {"private", "chatroom"} or not raw_id:
            continue
        source_ids.append(text)
        seen.add(text)

    source_rows = await _fetch_source_rows_by_ids(source_ids, user_name, ai_name)
    found_ids = {row["id"] for row in source_rows}
    source_ids = [source_id for source_id in source_ids if source_id in found_ids]
    source_rows.sort(key=lambda row: row["created_at"])
    source_start = source_rows[0]["created_at"] if source_rows else None
    source_end = source_rows[-1]["created_at"] if source_rows else None
    source_text = json.dumps(source_ids, ensure_ascii=False)

    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, source_msg_id, source_start_ts, source_end_ts FROM memories WHERE id=?",
            (mem_id,),
        )
        before = await cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="Memory not found")

        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_source_edit_log ("
            "id TEXT PRIMARY KEY, mem_id TEXT NOT NULL, before_json TEXT, after_json TEXT, created_at REAL NOT NULL)"
        )
        before_dict = dict(before)
        after_dict = {
            "id": mem_id,
            "source_msg_id": source_text,
            "source_start_ts": source_start,
            "source_end_ts": source_end,
        }
        await db.execute(
            "INSERT INTO memory_source_edit_log (id, mem_id, before_json, after_json, created_at) VALUES (?,?,?,?,?)",
            (
                f"mem_source_edit_{time.time_ns()}",
                mem_id,
                json.dumps(before_dict, ensure_ascii=False),
                json.dumps(after_dict, ensure_ascii=False),
                now,
            ),
        )
        await db.execute(
            "UPDATE memories SET source_msg_id=?, source_start_ts=?, source_end_ts=? WHERE id=?",
            (source_text, source_start, source_end, mem_id),
        )
        await db.commit()

    return {
        "ok": True,
        "id": mem_id,
        "source_msg_id": source_text,
        "source_start_ts": source_start,
        "source_end_ts": source_end,
        "selected_count": len(source_ids),
        **_memory_time_payload({"source_start_ts": source_start, "source_end_ts": source_end}),
    }
