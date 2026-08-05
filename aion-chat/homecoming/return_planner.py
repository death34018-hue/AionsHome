"""Read-only conflict planner for quarantined Homecoming operations."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass

from .contracts import canonical_json_bytes, sha256_hex


@dataclass(frozen=True)
class PlanRow:
    op_id: str
    device_seq: int
    decision: str
    reason: str
    effects: tuple[str, ...]
    effect_payload: dict


@dataclass(frozen=True)
class ImportPlan:
    plan_id: str
    package_id: str
    rows: tuple[PlanRow, ...]
    plan_sha256: str
    main_state_sha256: str
    counts: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "package_id": self.package_id,
            "plan_sha256": self.plan_sha256,
            "main_state_sha256": self.main_state_sha256,
            "counts": dict(self.counts),
            "rows": [
                {
                    "op_id": row.op_id,
                    "device_seq": row.device_seq,
                    "decision": row.decision,
                    "reason": row.reason,
                    "effects": list(row.effects),
                    "effect_payload": row.effect_payload,
                }
                for row in self.rows
            ],
        }


async def build_import_plan(db, stored_package: dict, now: float) -> ImportPlan:
    mapping = await _timeline_mapping(
        db,
        str(stored_package.get("device_id") or ""),
        str(stored_package.get("base_snapshot_id") or ""),
    )
    observations: list[object] = [{"timeline_mapping": mapping}]
    rows: list[PlanRow] = []
    planned_messages: dict[str, str] = {}
    planned_memories: set[str] = set()
    for operation in sorted(
        stored_package.get("operations") or [],
        key=lambda item: int(item["device_seq"]),
    ):
        entity = operation["entity_type"]
        if entity == "message":
            row = await _message_plan(db, operation, mapping, observations)
        elif entity == "memory":
            row = await _manual_memory_plan(db, operation, mapping, observations)
        elif entity == "memory_auto":
            row = await _automatic_memory_plan(
                db, operation, mapping, observations, planned_messages
            )
        elif entity == "summary_checkpoint":
            row = _summary_checkpoint_plan(
                operation,
                str(stored_package.get("epoch_id") or ""),
                planned_messages,
                planned_memories,
                observations,
            )
        elif entity == "schedule":
            row = await _schedule_plan(db, operation, mapping, observations, now)
        elif entity == "supervision_event":
            row = _row(operation, "apply", "history_only", ("audit",))
        elif entity == "deferred_control":
            row = _row(
                operation,
                "quarantine",
                "deferred_controls_require_manual_server_review",
            )
        else:
            row = _row(operation, "invalid", "unsupported_operation")
        rows.append(row)
        if entity == "message" and row.decision in {"apply", "duplicate"}:
            planned_messages[str(operation["entity_id"])] = str(
                operation["payload"].get("timeline_id") or ""
            )
        if entity == "memory_auto" and row.decision in {"apply", "duplicate"}:
            planned_memories.add(str(operation["entity_id"]))

    main_state_sha256 = sha256_hex(canonical_json_bytes(observations))
    plan_material = [
        {
            "op_id": row.op_id,
            "device_seq": row.device_seq,
            "decision": row.decision,
            "reason": row.reason,
            "effects": list(row.effects),
        }
        for row in rows
    ]
    plan_sha256 = sha256_hex(canonical_json_bytes(plan_material))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.decision] = counts.get(row.decision, 0) + 1
    package_id = str(stored_package["package_id"])
    return ImportPlan(
        plan_id=f"plan-{plan_sha256}-{main_state_sha256}",
        package_id=package_id,
        rows=tuple(rows),
        plan_sha256=plan_sha256,
        main_state_sha256=main_state_sha256,
        counts=counts,
    )


async def _message_plan(db, operation, mapping, observations) -> PlanRow:
    payload = operation["payload"]
    timeline = str(payload.get("timeline_id") or "")
    container = mapping.get(timeline)
    if not container:
        return _row(operation, "quarantine", "timeline_mapping_unavailable")
    table = "messages" if timeline == "main_private" else "chatroom_messages"
    container_column = "conv_id" if table == "messages" else "room_id"
    sender_column = "role" if table == "messages" else "sender"
    existing = await _one(
        db,
        f"SELECT id,{container_column},{sender_column},content,attachments,created_at "
        f"FROM {table} WHERE id=?",
        (operation["entity_id"],),
    )
    observations.append({"message": existing, "table": table})
    effect_payload = {
        "table": table,
        "container_id": container,
        "message": payload,
    }
    if existing is None:
        return _row(
            operation,
            "apply",
            "new_mapped_message",
            ("insert_message",),
            effect_payload,
        )
    expected_sender = (
        str(payload.get("role") or "")
        if table == "messages"
        else str(payload.get("sender_id") or payload.get("role") or "")
    )
    same = (
        existing[container_column] == container
        and _text(existing[sender_column]) == _text(expected_sender)
        and _text(existing["content"]) == _text(payload.get("text"))
        and not _has_server_attachments(existing.get("attachments"))
        and not payload.get("attachment_kind")
    )
    if same:
        return _row(operation, "duplicate", "same_message_already_exists")
    return _row(operation, "quarantine", "message_id_collision")


async def _manual_memory_plan(db, operation, mapping, observations) -> PlanRow:
    payload = operation["payload"]
    owner = str(payload.get("owner_id") or "")
    target = _memory_target(owner, mapping)
    if target is None:
        return _row(operation, "quarantine", "memory_owner_mapping_unavailable")
    table, room_id = target
    existing = await _one(
        db, f"SELECT * FROM {table} WHERE id=?", (operation["entity_id"],)
    )
    observations.append({"memory": _portable(existing), "table": table})
    action = operation["action"]
    if action == "delete" and existing is None:
        return _row(operation, "duplicate", "memory_is_already_absent")
    if action == "create" and existing is not None:
        if _text(existing.get("content")) == _text(payload.get("content")):
            return _row(operation, "duplicate", "same_memory_already_exists")
        return _row(operation, "server_wins", "new_memory_id_collision")
    effects = ("delete_memory",) if action == "delete" else ("upsert_memory",)
    effect_payload = {
        "table": table,
        "room_id": room_id,
        "memory": payload,
    }
    reason = "verified_manual_memory"
    if existing is not None and action in {"update", "delete"}:
        effects = effects + ("audit_preimage",)
        effect_payload["server_preimage"] = _portable(existing)
        reason = "verified_manual_memory_phone_wins_with_preimage"
    return _row(operation, "apply", reason, effects, effect_payload)


async def _automatic_memory_plan(
    db, operation, mapping, observations, planned_messages=None
) -> PlanRow:
    payload = operation["payload"]
    owner = str(payload.get("owner_id") or "")
    target = _memory_target(owner, mapping)
    if target is None:
        return _row(operation, "skip", "automatic_memory_owner_unavailable")
    source_ids = payload.get("source_message_ids")
    if not isinstance(source_ids, list) or not source_ids:
        return _row(operation, "skip", "automatic_memory_has_no_evidence")
    found = set()
    for table in ("messages", "chatroom_messages"):
        placeholders = ",".join("?" for _ in source_ids)
        cursor = await db.execute(
            f"SELECT id FROM {table} WHERE id IN ({placeholders})",
            tuple(source_ids),
        )
        found.update(str(row[0]) for row in await cursor.fetchall())
    found.update(
        message_id
        for message_id in map(str, source_ids)
        if message_id in (planned_messages or {})
    )
    observations.append({"automatic_memory_sources": sorted(found)})
    if found != set(map(str, source_ids)):
        return _row(operation, "skip", "automatic_memory_evidence_incomplete")
    table, room_id = target
    existing = await _one(
        db, f"SELECT * FROM {table} WHERE id=?", (operation["entity_id"],)
    )
    observations.append({"automatic_memory": _portable(existing), "table": table})
    if existing is not None:
        if _text(existing.get("content")) == _text(payload.get("content")):
            return _row(operation, "duplicate", "same_automatic_memory_exists")
        return _row(operation, "skip", "automatic_memory_overlap")
    return _row(
        operation,
        "apply",
        "automatic_memory_evidence_verified",
        ("insert_automatic_memory",),
        {"table": table, "room_id": room_id, "memory": payload},
    )


def _summary_checkpoint_plan(
    operation,
    package_epoch: str,
    planned_messages: dict[str, str],
    planned_memories: set[str],
    observations,
) -> PlanRow:
    payload = operation["payload"]
    checkpoint_id = str(payload.get("checkpoint_id") or "")
    owner = str(payload.get("owner_id") or "")
    epoch = str(payload.get("epoch_id") or "")
    source_ids = payload.get("source_message_ids")
    memory_ids = payload.get("memory_ids")
    if (
        checkpoint_id != str(operation["entity_id"])
        or epoch != package_epoch
        or owner not in {"main", "second"}
        or not isinstance(source_ids, list)
        or not source_ids
        or not isinstance(memory_ids, list)
    ):
        return _row(operation, "invalid", "summary_checkpoint_shape_invalid")
    normalized_sources = [str(item) for item in source_ids]
    normalized_memories = [str(item) for item in memory_ids]
    if (
        len(set(normalized_sources)) != len(normalized_sources)
        or len(set(normalized_memories)) != len(normalized_memories)
        or str(payload.get("last_message_id") or "") != normalized_sources[-1]
    ):
        return _row(operation, "invalid", "summary_checkpoint_sequence_invalid")
    core = dict(payload)
    supplied_hash = str(core.pop("payload_sha256", "") or "")
    if supplied_hash != sha256_hex(canonical_json_bytes(core)):
        return _row(operation, "invalid", "summary_checkpoint_hash_invalid")
    allowed = (
        {"main_private", "group"}
        if owner == "main"
        else {"companion_private", "group"}
    )
    if any(
        planned_messages.get(message_id) not in allowed
        for message_id in normalized_sources
    ):
        return _row(operation, "skip", "summary_checkpoint_sources_incomplete")
    if any(memory_id not in planned_memories for memory_id in normalized_memories):
        return _row(operation, "skip", "summary_checkpoint_memories_incomplete")
    observations.append(
        {
            "summary_checkpoint": checkpoint_id,
            "owner_id": owner,
            "source_message_ids": normalized_sources,
        }
    )
    return _row(
        operation,
        "apply",
        "summary_checkpoint_verified",
        ("insert_summary_coverage",),
        {
            "owner_id": owner,
            "epoch_id": epoch,
            "checkpoint_id": checkpoint_id,
            "source_message_ids": normalized_sources,
            "memory_ids": normalized_memories,
        },
    )
async def _schedule_plan(db, operation, mapping, observations, now: float) -> PlanRow:
    payload = operation["payload"]
    action = operation["action"]
    if action == "execute":
        return _row(operation, "apply", "schedule_history_only", ("audit",))
    schedule_id = str(payload.get("id") or operation["entity_id"])
    existing = await _one(
        db, "SELECT * FROM schedules WHERE id=?", (schedule_id,)
    )
    observations.append({"schedule": _portable(existing)})
    if action == "delete":
        if existing is None or str(existing.get("status") or "") != "active":
            return _row(operation, "duplicate", "schedule_is_not_active")
        return _row(operation, "server_wins", "server_schedule_state_wins")
    if existing is not None:
        return _row(operation, "server_wins", "schedule_id_already_exists")
    try:
        trigger = float(payload.get("trigger_at"))
    except (TypeError, ValueError):
        return _row(operation, "invalid", "schedule_trigger_is_invalid")
    if trigger > 100_000_000_000:
        trigger /= 1000.0
    if trigger <= float(now):
        return _row(operation, "skip", "schedule_trigger_is_past")
    timeline = str(payload.get("timeline_id") or "")
    if not mapping.get(timeline):
        return _row(operation, "quarantine", "schedule_timeline_unavailable")
    return _row(
        operation,
        "apply",
        "future_schedule_is_safe",
        ("insert_schedule",),
        {
            "schedule": payload,
            "container_id": mapping[timeline],
        },
    )


async def _timeline_mapping(db, device_id: str, snapshot_id: str) -> dict:
    try:
        cursor = await db.execute(
            "SELECT timeline_mapping_json FROM homecoming_snapshot_exports "
            "WHERE device_id=? AND snapshot_id=?",
            (device_id, snapshot_id),
        )
        row = await cursor.fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    try:
        value = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}
    allowed = {"main_private", "companion_private", "group"}
    return {
        key: str(item)
        for key, item in value.items()
        if key in allowed and isinstance(item, str) and item
    }


def _memory_target(owner: str, mapping: dict) -> tuple[str, str] | None:
    if owner == "main" and mapping.get("main_private"):
        return "memories", ""
    if owner == "second" and mapping.get("companion_private"):
        return "chatroom_memories", mapping["companion_private"]
    return None


async def _one(db, query: str, parameters: tuple) -> dict | None:
    cursor = await db.execute(query, parameters)
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [item[0] for item in cursor.description or ()]
    return dict(zip(columns, row))


def _row(
    operation,
    decision: str,
    reason: str,
    effects: tuple[str, ...] = (),
    effect_payload: dict | None = None,
) -> PlanRow:
    return PlanRow(
        op_id=str(operation["op_id"]),
        device_seq=int(operation["device_seq"]),
        decision=decision,
        reason=reason,
        effects=effects,
        effect_payload=effect_payload or {},
    )


def _text(value) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _has_server_attachments(raw) -> bool:
    try:
        return bool(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return bool(raw)


def _portable(row: dict | None):
    if row is None:
        return None
    return {
        key: value
        for key, value in row.items()
        if not isinstance(value, (bytes, bytearray))
    }


async def plan_stored_package(db, package_id: str, now: float) -> ImportPlan:
    from .return_store import ensure_return_tables

    await ensure_return_tables(db)
    cursor = await db.execute(
        "SELECT package_id,device_id,epoch_id,base_snapshot_id "
        "FROM homecoming_return_packages WHERE package_id=?",
        (package_id,),
    )
    package_row = await cursor.fetchone()
    if package_row is None:
        raise KeyError("return package was not found")
    cursor = await db.execute(
        "SELECT op_id,device_seq,entity_type,entity_id,action,"
        "base_revision,payload_json,created_at "
        "FROM homecoming_return_operations WHERE package_id=? "
        "ORDER BY device_seq",
        (package_id,),
    )
    operations = []
    for row in await cursor.fetchall():
        operations.append(
            {
                "op_id": row[0],
                "device_seq": row[1],
                "entity_type": row[2],
                "entity_id": row[3],
                "action": row[4],
                "base_revision": row[5],
                "payload": json.loads(row[6]),
                "created_at": row[7],
            }
        )
    plan = await build_import_plan(
        db,
        {
            "package_id": package_row[0],
            "device_id": package_row[1],
            "epoch_id": package_row[2],
            "base_snapshot_id": package_row[3],
            "operations": operations,
        },
        now,
    )
    await db.execute(
        "UPDATE homecoming_import_sessions SET state='stale',updated_at=? "
        "WHERE package_id=? AND state='planned' AND import_session_id<>?",
        (float(now), package_id, plan.plan_id),
    )
    await db.execute(
        "INSERT OR IGNORE INTO homecoming_import_sessions "
        "(import_session_id,package_id,plan_sha256,main_state_sha256,"
        "counts_json,state,accepted_highest_device_seq,"
        "result_summary_sha256,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'planned',0,'',?,?)",
        (
            plan.plan_id,
            package_id,
            plan.plan_sha256,
            plan.main_state_sha256,
            json.dumps(plan.counts, sort_keys=True, separators=(",", ":")),
            float(now),
            float(now),
        ),
    )
    for row in plan.rows:
        await db.execute(
            "INSERT OR IGNORE INTO homecoming_import_results "
            "(import_session_id,op_id,device_seq,decision,reason,"
            "result_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                plan.plan_id,
                row.op_id,
                row.device_seq,
                row.decision,
                row.reason,
                json.dumps(
                    {
                        "effects": list(row.effects),
                        "effect_payload": row.effect_payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                float(now),
            ),
        )
    await db.execute(
        "UPDATE homecoming_return_packages SET state='planned',updated_at=? "
        "WHERE package_id=?",
        (float(now), package_id),
    )
    return plan
