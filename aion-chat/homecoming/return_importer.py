"""Bounded, resumable SQL importer for approved Homecoming plans."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

from .contracts import canonical_json_bytes, sha256_hex
from .return_planner import build_import_plan
from .return_store import ensure_return_tables


class StaleImportPlan(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportReceipt:
    import_session_id: str
    package_id: str
    accepted_highest_device_seq: int
    counts: dict[str, int]
    result_summary_sha256: str
    complete: bool
    retryable: bool

    def to_dict(self) -> dict:
        return {
            "import_session_id": self.import_session_id,
            "package_id": self.package_id,
            "accepted_highest_device_seq": self.accepted_highest_device_seq,
            "counts": dict(self.counts),
            "result_summary_sha256": self.result_summary_sha256,
            "complete": self.complete,
            "retryable": self.retryable,
        }


async def apply_import_plan(
    db_factory,
    plan_id: str,
    now: float,
    max_rows: int = 50,
    budget_ms: int = 250,
    *,
    clock=time.monotonic,
    publisher=None,
) -> ImportReceipt:
    if not 1 <= int(max_rows) <= 50:
        raise ValueError("max_rows must be between 1 and 50")
    if not 1 <= int(budget_ms) <= 250:
        raise ValueError("budget_ms must be between 1 and 250")
    events: list[dict] = []
    try:
        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_return_tables(db)
            session = await _session(db, plan_id)
            if session is None:
                await db.rollback()
                raise KeyError("import plan was not found")
            counts = json.loads(session["counts_json"] or "{}")
            if session["state"] == "confirmed":
                await db.rollback()
                return _receipt(session, counts, complete=True, retryable=False)
            if session["state"] == "stale":
                await db.rollback()
                raise StaleImportPlan("import plan is stale")

            package = await _stored_package(db, session["package_id"])
            current = await build_import_plan(db, package, now)
            cursor = int(session["accepted_highest_device_seq"])
            if cursor == 0:
                valid = (
                    current.plan_sha256 == session["plan_sha256"]
                    and current.main_state_sha256 == session["main_state_sha256"]
                )
            else:
                valid = await _pending_plan_matches(db, plan_id, current, cursor)
            if not valid:
                await db.execute(
                    "UPDATE homecoming_import_sessions "
                    "SET state='stale',updated_at=? WHERE import_session_id=?",
                    (float(now), plan_id),
                )
                await db.commit()
                raise StaleImportPlan("mainline changed after dry-run")

            rows = await _pending_rows(db, plan_id, cursor, int(max_rows))
            started = clock()
            accepted = cursor
            processed = 0
            for row in rows:
                if processed > 0 and (clock() - started) * 1000 >= budget_ms:
                    break
                result = json.loads(row["result_json"])
                if row["decision"] == "apply":
                    await _apply_effects(
                        db,
                        tuple(result.get("effects") or ()),
                        result.get("effect_payload") or {},
                        float(now),
                        events,
                        plan_id,
                    )
                await db.execute(
                    "UPDATE homecoming_import_results "
                    "SET apply_state='completed',applied_at=? "
                    "WHERE import_session_id=? AND op_id=?",
                    (float(now), plan_id, row["op_id"]),
                )
                await db.execute(
                    "UPDATE homecoming_return_operations SET state='processed' "
                    "WHERE op_id=?",
                    (row["op_id"],),
                )
                accepted = int(row["device_seq"])
                processed += 1

            highest = int(package["highest_device_seq"])
            complete = accepted >= highest
            summary = ""
            state = "confirmed" if complete else "applying"
            if complete:
                summary = await _result_hash(db, plan_id, counts)
            await db.execute(
                "UPDATE homecoming_import_sessions SET state=?,"
                "accepted_highest_device_seq=?,result_summary_sha256=?,"
                "updated_at=? WHERE import_session_id=?",
                (state, accepted, summary, float(now), plan_id),
            )
            await db.execute(
                "UPDATE homecoming_return_packages SET state=?,updated_at=? "
                "WHERE package_id=?",
                (state, float(now), session["package_id"]),
            )
            await db.commit()
            receipt = ImportReceipt(
                import_session_id=plan_id,
                package_id=session["package_id"],
                accepted_highest_device_seq=accepted,
                counts=counts,
                result_summary_sha256=summary,
                complete=complete,
                retryable=False,
            )
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        return ImportReceipt(
            import_session_id=plan_id,
            package_id="",
            accepted_highest_device_seq=0,
            counts={},
            result_summary_sha256="",
            complete=False,
            retryable=True,
        )

    publish = publisher or _default_publisher
    if events:
        try:
            await publish(events)
        except Exception:
            pass
    return receipt


async def _session(db, plan_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT import_session_id,package_id,plan_sha256,"
        "main_state_sha256,counts_json,state,"
        "accepted_highest_device_seq,result_summary_sha256 "
        "FROM homecoming_import_sessions WHERE import_session_id=?",
        (plan_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    names = (
        "import_session_id",
        "package_id",
        "plan_sha256",
        "main_state_sha256",
        "counts_json",
        "state",
        "accepted_highest_device_seq",
        "result_summary_sha256",
    )
    return dict(zip(names, row))


async def _stored_package(db, package_id: str) -> dict:
    cursor = await db.execute(
        "SELECT package_id,device_id,epoch_id,base_snapshot_id,highest_device_seq "
        "FROM homecoming_return_packages WHERE package_id=?",
        (package_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise KeyError("return package was not found")
    cursor = await db.execute(
        "SELECT op_id,device_seq,entity_type,entity_id,action,"
        "base_revision,payload_json,created_at "
        "FROM homecoming_return_operations WHERE package_id=? "
        "ORDER BY device_seq",
        (package_id,),
    )
    operations = [
        {
            "op_id": item[0],
            "device_seq": item[1],
            "entity_type": item[2],
            "entity_id": item[3],
            "action": item[4],
            "base_revision": item[5],
            "payload": json.loads(item[6]),
            "created_at": item[7],
        }
        for item in await cursor.fetchall()
    ]
    return {
        "package_id": row[0],
        "device_id": row[1],
        "epoch_id": row[2],
        "base_snapshot_id": row[3],
        "highest_device_seq": row[4],
        "operations": operations,
    }


async def _pending_plan_matches(db, plan_id: str, current, cursor: int) -> bool:
    expected = {
        row.device_seq: (
            row.decision,
            row.reason,
            tuple(row.effects),
            row.effect_payload,
        )
        for row in current.rows
        if row.device_seq > cursor
    }
    db_cursor = await db.execute(
        "SELECT device_seq,decision,reason,result_json "
        "FROM homecoming_import_results "
        "WHERE import_session_id=? AND device_seq>? ORDER BY device_seq",
        (plan_id, cursor),
    )
    stored = {}
    for sequence, decision, reason, result_json in await db_cursor.fetchall():
        result = json.loads(result_json)
        stored[int(sequence)] = (
            decision,
            reason,
            tuple(result.get("effects") or ()),
            result.get("effect_payload") or {},
        )
    return stored == expected


async def _pending_rows(db, plan_id: str, cursor: int, limit: int) -> list[dict]:
    db_cursor = await db.execute(
        "SELECT op_id,device_seq,decision,reason,result_json "
        "FROM homecoming_import_results "
        "WHERE import_session_id=? AND device_seq>? "
        "ORDER BY device_seq LIMIT ?",
        (plan_id, cursor, limit),
    )
    names = ("op_id", "device_seq", "decision", "reason", "result_json")
    return [dict(zip(names, row)) for row in await db_cursor.fetchall()]


async def _apply_effects(
    db,
    effects,
    payload,
    now: float,
    events: list[dict],
    import_session_id: str,
):
    if "insert_message" in effects:
        await _insert_message(db, payload, events)
    if "delete_memory" in effects:
        await _delete_memory(db, payload, events)
    if "upsert_memory" in effects or "insert_automatic_memory" in effects:
        await _upsert_memory(db, payload, now, events)
    if "insert_schedule" in effects:
        await _insert_schedule(db, payload, now, events)
    if "insert_summary_coverage" in effects:
        await _insert_summary_coverage(
            db, payload, import_session_id, now
        )


async def _insert_summary_coverage(
    db, payload: dict, import_session_id: str, now: float
):
    source_ids = [str(item) for item in payload.get("source_message_ids") or []]
    memory_ids = [str(item) for item in payload.get("memory_ids") or []]
    if not source_ids:
        raise ValueError("summary coverage has no source messages")
    placeholders = ",".join("?" for _ in source_ids)
    found_messages = set()
    for table in ("messages", "chatroom_messages"):
        cursor = await db.execute(
            f"SELECT id FROM {table} WHERE id IN ({placeholders})",
            tuple(source_ids),
        )
        found_messages.update(str(row[0]) for row in await cursor.fetchall())
    if found_messages != set(source_ids):
        raise ValueError("summary coverage source messages are incomplete")
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        found_memories = set()
        for table in ("memories", "chatroom_memories"):
            cursor = await db.execute(
                f"SELECT id FROM {table} WHERE id IN ({placeholders})",
                tuple(memory_ids),
            )
            found_memories.update(str(row[0]) for row in await cursor.fetchall())
        if found_memories != set(memory_ids):
            raise ValueError("summary coverage memories are incomplete")
    await db.executemany(
        "INSERT OR IGNORE INTO homecoming_summary_coverage "
        "(owner_id,message_id,epoch_id,checkpoint_id,import_session_id,verified_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            (
                str(payload["owner_id"]),
                message_id,
                str(payload["epoch_id"]),
                str(payload["checkpoint_id"]),
                import_session_id,
                float(now),
            )
            for message_id in source_ids
        ],
    )


async def _insert_message(db, effect, events):
    table = effect["table"]
    payload = effect["message"]
    message_id = str(payload["id"])
    content = str(payload.get("text") or "")
    if not content:
        content = str(payload.get("attachment_transcript") or "")
    created_at = _server_seconds(payload.get("created_at"))
    if table == "messages":
        role = str(payload.get("role") or "user")
        await db.execute(
            "INSERT INTO messages "
            "(id,conv_id,role,content,attachments,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                message_id,
                effect["container_id"],
                role,
                content,
                "[]",
                created_at,
            ),
        )
        await db.execute(
            "UPDATE conversations SET updated_at=MAX(updated_at,?) WHERE id=?",
            (created_at, effect["container_id"]),
        )
        event = {
            "type": "msg_created",
            "data": {
                "id": message_id,
                "conv_id": effect["container_id"],
                "role": role,
                "content": content,
                "attachments": "[]",
                "created_at": created_at,
            },
        }
    else:
        raw_sender = str(payload.get("sender_id") or payload.get("role") or "user")
        sender = {"main": "aion", "second": "connor"}.get(
            raw_sender, raw_sender
        )
        await db.execute(
            "INSERT INTO chatroom_messages "
            "(id,room_id,sender,content,attachments,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                message_id,
                effect["container_id"],
                sender,
                content,
                "[]",
                created_at,
            ),
        )
        await db.execute(
            "UPDATE chatroom_rooms SET updated_at=MAX(updated_at,?) WHERE id=?",
            (created_at, effect["container_id"]),
        )
        event = {
            "type": "chatroom_msg_created",
            "data": {
                "id": message_id,
                "room_id": effect["container_id"],
                "sender": sender,
                "content": content,
                "attachments": "[]",
                "created_at": created_at,
            },
        }
    events.append(await _durable_event(db, event))


def _server_seconds(value) -> float:
    timestamp = float(value or 0)
    if timestamp > 100_000_000_000:
        timestamp /= 1000.0
    return timestamp


async def _upsert_memory(db, effect, now: float, events):
    table = effect["table"]
    memory = effect["memory"]
    memory_id = str(memory["id"])
    content = str(memory.get("content") or "")
    keywords = str(memory.get("keywords") or "")
    importance = float(memory.get("importance") or 0.5)
    created_at = float(memory.get("updated_at") or now)
    if table == "memories":
        cursor = await db.execute(
            "UPDATE memories SET content=?,type=?,keywords=?,importance=?,"
            "archive_state='active' WHERE id=?",
            (
                content,
                str(memory.get("type") or "event"),
                keywords,
                importance,
                memory_id,
            ),
        )
        if cursor.rowcount == 0:
            await db.execute(
                "INSERT INTO memories "
                "(id,content,type,created_at,source_conv,embedding,keywords,"
                "importance,source_msg_id,archive_state) "
                "VALUES (?,?,?,?,?,NULL,?,?,?,'active')",
                (
                    memory_id,
                    content,
                    str(memory.get("type") or "event"),
                    created_at,
                    None,
                    keywords,
                    importance,
                    _first_source(memory),
                ),
            )
        event_type = "memory_updated" if cursor.rowcount else "memory_added"
        data = {"id": memory_id, "content": content, "keywords": keywords}
    else:
        cursor = await db.execute(
            "UPDATE chatroom_memories SET content=?,keywords=?,importance=?,"
            "archive_state='active' WHERE id=?",
            (content, keywords, importance, memory_id),
        )
        if cursor.rowcount == 0:
            await db.execute(
                "INSERT INTO chatroom_memories "
                "(id,room_id,scope,content,keywords,importance,embedding,"
                "created_at,source_msg_id,archive_state) "
                "VALUES (?,?,?,?,?,?,NULL,?,?,'active')",
                (
                    memory_id,
                    effect["room_id"],
                    "connor",
                    content,
                    keywords,
                    importance,
                    created_at,
                    _first_source(memory),
                ),
            )
        event_type = "memory_collection_changed"
        data = {"id": memory_id, "room_id": effect["room_id"]}
    events.append(await _durable_event(
        db, {"type": event_type, "data": data}
    ))


async def _delete_memory(db, effect, events):
    table = effect["table"]
    memory_id = str(effect["memory"]["id"])
    await db.execute(f"DELETE FROM {table} WHERE id=?", (memory_id,))
    event = {
        "type": "memory_deleted"
        if table == "memories"
        else "memory_collection_changed",
        "data": {"id": memory_id, "room_id": effect.get("room_id") or ""},
    }
    events.append(await _durable_event(db, event))


async def _insert_schedule(db, effect, now: float, events):
    schedule = effect["schedule"]
    trigger = float(schedule["trigger_at"])
    if trigger > 100_000_000_000:
        trigger /= 1000.0
    trigger_text = datetime.fromtimestamp(trigger).strftime("%Y-%m-%d %H:%M:%S")
    owner = str(schedule.get("owner_id") or "main")
    origin = "connor" if owner == "second" else "aion"
    room_id = "" if owner == "main" else str(effect["container_id"])
    await db.execute(
        "INSERT INTO schedules "
        "(id,type,trigger_at,content,created_at,status,origin,origin_room_id) "
        "VALUES (?,?,?,?,?,'active',?,?)",
        (
            str(schedule["id"]),
            str(schedule.get("type") or "reminder"),
            trigger_text,
            str(schedule.get("content") or ""),
            float(schedule.get("updated_at") or now),
            origin,
            room_id,
        ),
    )
    events.append({"type": "schedule_changed", "data": {"id": schedule["id"]}})


async def _durable_event(db, event: dict) -> dict:
    from sync_events import append_sync_event, attach_sync_seq

    sequence = await append_sync_event(db, event)
    return attach_sync_seq(event, sequence)


async def _result_hash(db, plan_id: str, counts: dict) -> str:
    cursor = await db.execute(
        "SELECT op_id,device_seq,decision,reason,apply_state "
        "FROM homecoming_import_results WHERE import_session_id=? "
        "ORDER BY device_seq",
        (plan_id,),
    )
    rows = [
        {
            "op_id": row[0],
            "device_seq": row[1],
            "decision": row[2],
            "reason": row[3],
            "apply_state": row[4],
        }
        for row in await cursor.fetchall()
    ]
    return sha256_hex(canonical_json_bytes({"counts": counts, "rows": rows}))


def _first_source(memory: dict):
    values = memory.get("source_message_ids")
    return str(values[0]) if isinstance(values, list) and values else None


def _receipt(session: dict, counts: dict, *, complete: bool, retryable: bool):
    return ImportReceipt(
        import_session_id=session["import_session_id"],
        package_id=session["package_id"],
        accepted_highest_device_seq=int(
            session["accepted_highest_device_seq"]
        ),
        counts=counts,
        result_summary_sha256=session["result_summary_sha256"],
        complete=complete,
        retryable=retryable,
    )


async def _default_publisher(events: list[dict]):
    from ws import manager

    for event in events:
        await manager.broadcast(event)
