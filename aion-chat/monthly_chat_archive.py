"""One-time, transactional monthly archive for private and group chat windows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ArchiveReport:
    backup_path: Path
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    targets: dict[str, dict[tuple[int, int], str]]


def local_month(timestamp: float, timezone_name: str) -> tuple[int, int]:
    dt = datetime.fromtimestamp(timestamp, ZoneInfo(timezone_name))
    return dt.year, dt.month


def window_title(kind: str, year: int, month: int) -> str:
    if kind == "group":
        return f"群聊{year % 100:02d}-{month}"
    prefix = {"aion": "Aion", "connor": "Connor"}[kind]
    return f"{prefix} {year % 100:02d}-{month}"


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        "aion": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "connor": db.execute(
            "SELECT COUNT(*) FROM chatroom_messages m JOIN chatroom_rooms r ON r.id=m.room_id WHERE r.type='connor_1v1'"
        ).fetchone()[0],
        "group": db.execute(
            "SELECT COUNT(*) FROM chatroom_messages m JOIN chatroom_rooms r ON r.id=m.room_id WHERE r.type='group'"
        ).fetchone()[0],
    }


def _latest_id(db: sqlite3.Connection, kind: str) -> str:
    if kind == "aion":
        row = db.execute(
            "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    else:
        room_type = "connor_1v1" if kind == "connor" else "group"
        row = db.execute(
            "SELECT id FROM chatroom_rooms WHERE type=? ORDER BY updated_at DESC LIMIT 1",
            (room_type,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"Missing active {kind} window")
    return row[0]


def _message_rows(db: sqlite3.Connection, kind: str) -> list[tuple[str, float]]:
    if kind == "aion":
        return db.execute("SELECT id, created_at FROM messages").fetchall()
    room_type = "connor_1v1" if kind == "connor" else "group"
    return db.execute(
        "SELECT m.id, m.created_at FROM chatroom_messages m "
        "JOIN chatroom_rooms r ON r.id=m.room_id WHERE r.type=?",
        (room_type,),
    ).fetchall()


def _make_targets(
    db: sqlite3.Connection,
    kind: str,
    rows: list[tuple[str, float]],
    active_id: str,
    timezone_name: str,
) -> dict[tuple[int, int], str]:
    months = sorted({local_month(ts, timezone_name) for _, ts in rows})
    if not months:
        return {}
    latest_month = months[-1]
    targets: dict[tuple[int, int], str] = {}
    if kind == "aion":
        model = db.execute(
            "SELECT model FROM conversations WHERE id=?", (active_id,)
        ).fetchone()[0]
        for year, month in months:
            target_id = active_id if (year, month) == latest_month else f"conv_month_aion_{year}_{month:02d}"
            targets[(year, month)] = target_id
            db.execute(
                "INSERT INTO conversations (id,title,model,created_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, model=excluded.model",
                (target_id, window_title(kind, year, month), model, 0.0, 0.0),
            )
    else:
        room_type = "connor_1v1" if kind == "connor" else "group"
        template = db.execute(
            "SELECT aion_persona,connor_persona,context_minutes,ai_chat_rounds FROM chatroom_rooms WHERE id=?",
            (active_id,),
        ).fetchone()
        for year, month in months:
            target_id = active_id if (year, month) == latest_month else f"cr_month_{kind}_{year}_{month:02d}"
            targets[(year, month)] = target_id
            db.execute(
                "INSERT INTO chatroom_rooms "
                "(id,title,type,aion_persona,connor_persona,context_minutes,ai_chat_rounds,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,type=excluded.type",
                (
                    target_id,
                    window_title(kind, year, month),
                    room_type,
                    template[0],
                    template[1],
                    template[2],
                    template[3],
                    0.0,
                    0.0,
                ),
            )
    return targets


def _repair_direct_references(db: sqlite3.Connection) -> None:
    statements = {
        "heart_whispers": "UPDATE heart_whispers SET conv_id=(SELECT conv_id FROM messages WHERE id=heart_whispers.msg_id) WHERE EXISTS (SELECT 1 FROM messages WHERE id=heart_whispers.msg_id)",
        "memory_sources": (
            "UPDATE memory_sources SET "
            "conversation_id=COALESCE((SELECT conv_id FROM messages WHERE id=memory_sources.message_id),conversation_id), "
            "room_id=COALESCE((SELECT room_id FROM chatroom_messages WHERE id=memory_sources.message_id),room_id) "
            "WHERE EXISTS (SELECT 1 FROM messages WHERE id=memory_sources.message_id) "
            "OR EXISTS (SELECT 1 FROM chatroom_messages WHERE id=memory_sources.message_id)"
        ),
        "persona_evolution_items": (
            "UPDATE persona_evolution_items SET "
            "conv_id=COALESCE((SELECT conv_id FROM messages WHERE id=persona_evolution_items.message_id),conv_id), "
            "room_id=COALESCE((SELECT room_id FROM chatroom_messages WHERE id=persona_evolution_items.message_id),room_id) "
            "WHERE EXISTS (SELECT 1 FROM messages WHERE id=persona_evolution_items.message_id) "
            "OR EXISTS (SELECT 1 FROM chatroom_messages WHERE id=persona_evolution_items.message_id)"
        ),
        "health_miband_commands": (
            "UPDATE health_miband_commands SET source_id=COALESCE("
            "(SELECT conv_id FROM messages WHERE id=health_miband_commands.source_msg_id),"
            "(SELECT room_id FROM chatroom_messages WHERE id=health_miband_commands.source_msg_id),source_id) "
            "WHERE EXISTS (SELECT 1 FROM messages WHERE id=health_miband_commands.source_msg_id) "
            "OR EXISTS (SELECT 1 FROM chatroom_messages WHERE id=health_miband_commands.source_msg_id)"
        ),
        "message_ingress_dedupe": (
            "UPDATE message_ingress_dedupe SET target_id=COALESCE("
            "(SELECT conv_id FROM messages WHERE id=message_ingress_dedupe.message_id),"
            "(SELECT room_id FROM chatroom_messages WHERE id=message_ingress_dedupe.message_id),target_id) "
            "WHERE EXISTS (SELECT 1 FROM messages WHERE id=message_ingress_dedupe.message_id) "
            "OR EXISTS (SELECT 1 FROM chatroom_messages WHERE id=message_ingress_dedupe.message_id)"
        ),
        "memories": "UPDATE memories SET source_conv=(SELECT conv_id FROM messages WHERE id=memories.source_msg_id) WHERE EXISTS (SELECT 1 FROM messages WHERE id=memories.source_msg_id)",
        "moments": "UPDATE moments SET source_conv=(SELECT conv_id FROM messages WHERE id=moments.source_msg_id) WHERE EXISTS (SELECT 1 FROM messages WHERE id=moments.source_msg_id)",
        "chatroom_memories": "UPDATE chatroom_memories SET room_id=(SELECT room_id FROM chatroom_messages WHERE id=chatroom_memories.source_msg_id) WHERE EXISTS (SELECT 1 FROM chatroom_messages WHERE id=chatroom_memories.source_msg_id)",
    }
    for table, sql in statements.items():
        if _table_exists(db, table):
            db.execute(sql)


def _repair_time_references(
    db: sqlite3.Connection,
    old_conversation_created_at: dict[str, float],
    old_room_meta: dict[str, tuple[str, float]],
    targets: dict[str, dict[tuple[int, int], str]],
    timezone_name: str,
) -> None:
    orphan_room_hints: dict[str, tuple[str, float | None]] = {}
    if _table_exists(db, "chatroom_memories"):
        for room_id, scope, timestamp in db.execute(
            "SELECT room_id,scope,MIN(COALESCE(source_start_ts,created_at)) "
            "FROM chatroom_memories GROUP BY room_id,scope"
        ).fetchall():
            if scope in {"connor", "group"}:
                orphan_room_hints[room_id] = (scope, timestamp)

    def aion_target(old_id: str, timestamp: float | None) -> str | None:
        effective_ts = timestamp if timestamp is not None else old_conversation_created_at.get(old_id)
        return targets["aion"].get(local_month(effective_ts, timezone_name)) if effective_ts is not None else None

    def room_target(old_id: str, timestamp: float | None, scope: str = "") -> str | None:
        room_type, room_created_at = old_room_meta.get(old_id, ("", 0.0))
        kind = "connor" if room_type == "connor_1v1" else "group" if room_type == "group" else None
        if kind is None and scope in {"connor", "group"}:
            kind = scope
        hinted_scope, hinted_timestamp = orphan_room_hints.get(old_id, ("", None))
        if kind is None and hinted_scope:
            kind = hinted_scope
        effective_ts = timestamp if timestamp is not None else room_created_at or hinted_timestamp
        if kind is None or effective_ts is None:
            return None
        return targets.get(kind, {}).get(local_month(effective_ts, timezone_name))

    kept_conversations = set(targets["aion"].values())
    kept_rooms = set(targets["connor"].values()) | set(targets["group"].values())

    if _table_exists(db, "heart_whispers"):
        for row_id, old_id, created_at in db.execute(
            "SELECT id,conv_id,created_at FROM heart_whispers WHERE conv_id LIKE 'conv_%'"
        ).fetchall():
            if old_id not in kept_conversations:
                target = aion_target(old_id, created_at)
                if target:
                    db.execute("UPDATE heart_whispers SET conv_id=? WHERE id=?", (target, row_id))

    if _table_exists(db, "memory_sources"):
        for row_id, old_id, source_created_at in db.execute(
            "SELECT id,conversation_id,source_created_at FROM memory_sources WHERE conversation_id LIKE 'conv_%'"
        ).fetchall():
            if old_id not in kept_conversations:
                target = aion_target(old_id, source_created_at)
                if target:
                    db.execute("UPDATE memory_sources SET conversation_id=? WHERE id=?", (target, row_id))
        for row_id, old_id, source_created_at in db.execute(
            "SELECT id,room_id,source_created_at FROM memory_sources WHERE room_id LIKE 'cr_%'"
        ).fetchall():
            if old_id not in kept_rooms:
                target = room_target(old_id, source_created_at)
                if target:
                    db.execute("UPDATE memory_sources SET room_id=? WHERE id=?", (target, row_id))

    for table in ("memories", "moments"):
        if _table_exists(db, table):
            for row_id, old_id, created_at in db.execute(
                f"SELECT id,source_conv,created_at FROM {table} WHERE source_conv LIKE 'conv_%'"
            ).fetchall():
                if old_id not in kept_conversations:
                    target = aion_target(old_id, created_at)
                    if target:
                        db.execute(f"UPDATE {table} SET source_conv=? WHERE id=?", (target, row_id))

    if _table_exists(db, "schedules"):
        for row_id, old_id, created_at in db.execute(
            "SELECT id,origin_room_id,created_at FROM schedules WHERE origin_room_id<>''"
        ).fetchall():
            target = room_target(old_id, created_at)
            if target:
                db.execute("UPDATE schedules SET origin_room_id=? WHERE id=?", (target, row_id))

    if _table_exists(db, "chatroom_memories"):
        for row_id, old_id, timestamp, scope in db.execute(
            "SELECT id,room_id,COALESCE(source_start_ts,created_at),scope FROM chatroom_memories"
        ).fetchall():
            target = room_target(old_id, timestamp, scope or "")
            if target:
                db.execute("UPDATE chatroom_memories SET room_id=? WHERE id=?", (target, row_id))

    if _table_exists(db, "memory_recall_shadow_audit"):
        for row_id, old_id, created_at in db.execute(
            "SELECT id,room_id,created_at FROM memory_recall_shadow_audit WHERE room_id IS NOT NULL"
        ).fetchall():
            target = room_target(old_id, created_at)
            if target:
                db.execute("UPDATE memory_recall_shadow_audit SET room_id=? WHERE id=?", (target, row_id))

    if _table_exists(db, "idle_events"):
        for row_id, old_id, created_at in db.execute(
            "SELECT id,target_id,created_at FROM idle_events WHERE target_type='chatroom'"
        ).fetchall():
            target = room_target(old_id, created_at)
            if target:
                db.execute("UPDATE idle_events SET target_id=? WHERE id=?", (target, row_id))

    if _table_exists(db, "chatroom_digest_anchors"):
        merged: dict[str, float] = {}
        old_ids: list[str] = []
        for old_id, anchor_ts in db.execute(
            "SELECT room_id,anchor_ts FROM chatroom_digest_anchors"
        ).fetchall():
            target = room_target(old_id, anchor_ts)
            if target:
                old_ids.append(old_id)
                merged[target] = max(anchor_ts, merged.get(target, 0.0))
        for old_id in old_ids:
            db.execute("DELETE FROM chatroom_digest_anchors WHERE room_id=?", (old_id,))
        for target, anchor_ts in merged.items():
            db.execute(
                "INSERT INTO chatroom_digest_anchors(room_id,anchor_ts) VALUES (?,?) "
                "ON CONFLICT(room_id) DO UPDATE SET anchor_ts=MAX(anchor_ts,excluded.anchor_ts)",
                (target, anchor_ts),
            )


def archive_chat_windows(
    db_path: Path,
    backup_dir: Path,
    timezone_name: str = "Asia/Shanghai",
) -> ArchiveReport:
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"chat-before-monthly-archive-{stamp}.db"

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys=ON")
    backup = sqlite3.connect(backup_path)
    try:
        db.backup(backup)
    finally:
        backup.close()

    try:
        baseline_foreign_key_violations = set(db.execute("PRAGMA foreign_key_check").fetchall())
        db.execute("BEGIN IMMEDIATE")
        before_counts = _counts(db)
        old_conversation_created_at = dict(
            db.execute("SELECT id,created_at FROM conversations").fetchall()
        )
        old_room_meta = {
            row_id: (room_type, created_at)
            for row_id, room_type, created_at in db.execute(
                "SELECT id,type,created_at FROM chatroom_rooms"
            ).fetchall()
        }
        active_ids = {kind: _latest_id(db, kind) for kind in ("aion", "connor", "group")}
        rows_by_kind = {kind: _message_rows(db, kind) for kind in ("aion", "connor", "group")}
        targets = {
            kind: _make_targets(db, kind, rows_by_kind[kind], active_ids[kind], timezone_name)
            for kind in ("aion", "connor", "group")
        }

        for msg_id, timestamp in rows_by_kind["aion"]:
            db.execute(
                "UPDATE messages SET conv_id=? WHERE id=?",
                (targets["aion"][local_month(timestamp, timezone_name)], msg_id),
            )
        for kind in ("connor", "group"):
            for msg_id, timestamp in rows_by_kind[kind]:
                db.execute(
                    "UPDATE chatroom_messages SET room_id=? WHERE id=?",
                    (targets[kind][local_month(timestamp, timezone_name)], msg_id),
                )

        _repair_direct_references(db)
        _repair_time_references(
            db,
            old_conversation_created_at,
            old_room_meta,
            targets,
            timezone_name,
        )

        kept_conversations = set(targets["aion"].values())
        kept_rooms = set(targets["connor"].values()) | set(targets["group"].values())
        for (conv_id,) in db.execute("SELECT id FROM conversations").fetchall():
            if conv_id not in kept_conversations:
                if db.execute("SELECT COUNT(*) FROM messages WHERE conv_id=?", (conv_id,)).fetchone()[0]:
                    raise AssertionError(f"Legacy conversation still contains messages: {conv_id}")
                db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        for (room_id,) in db.execute("SELECT id FROM chatroom_rooms").fetchall():
            if room_id not in kept_rooms:
                if db.execute("SELECT COUNT(*) FROM chatroom_messages WHERE room_id=?", (room_id,)).fetchone()[0]:
                    raise AssertionError(f"Legacy room still contains messages: {room_id}")
                db.execute("DELETE FROM chatroom_rooms WHERE id=?", (room_id,))

        for conv_id in kept_conversations:
            first_ts, last_ts = db.execute(
                "SELECT MIN(created_at),MAX(created_at) FROM messages WHERE conv_id=?", (conv_id,)
            ).fetchone()
            db.execute(
                "UPDATE conversations SET created_at=?,updated_at=? WHERE id=?",
                (first_ts, last_ts, conv_id),
            )
        for room_id in kept_rooms:
            first_ts, last_ts = db.execute(
                "SELECT MIN(created_at),MAX(created_at) FROM chatroom_messages WHERE room_id=?", (room_id,)
            ).fetchone()
            db.execute(
                "UPDATE chatroom_rooms SET created_at=?,updated_at=? WHERE id=?",
                (first_ts, last_ts, room_id),
            )

        after_counts = _counts(db)
        if before_counts != after_counts:
            raise AssertionError(f"Message counts changed: {before_counts} -> {after_counts}")
        final_foreign_key_violations = set(db.execute("PRAGMA foreign_key_check").fetchall())
        new_foreign_key_violations = final_foreign_key_violations - baseline_foreign_key_violations
        if new_foreign_key_violations:
            raise AssertionError(f"New foreign key violations: {sorted(new_foreign_key_violations)}")
        chat_fk_violations = {
            row for row in final_foreign_key_violations if row[0] in {"messages", "chatroom_messages"}
        }
        if chat_fk_violations:
            raise AssertionError(f"Chat foreign key violations remain: {sorted(chat_fk_violations)}")
        if db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] != len(kept_conversations):
            raise AssertionError("Unexpected conversation inventory")
        if db.execute("SELECT COUNT(*) FROM chatroom_rooms").fetchone()[0] != len(kept_rooms):
            raise AssertionError("Unexpected room inventory")
        for kind, rows in rows_by_kind.items():
            table = "messages" if kind == "aion" else "chatroom_messages"
            id_col = "conv_id" if kind == "aion" else "room_id"
            for msg_id, timestamp in rows:
                actual = db.execute(f"SELECT {id_col} FROM {table} WHERE id=?", (msg_id,)).fetchone()[0]
                expected = targets[kind][local_month(timestamp, timezone_name)]
                if actual != expected:
                    raise AssertionError(f"Wrong month target for {msg_id}: {actual} != {expected}")
        db.commit()
        return ArchiveReport(backup_path, before_counts, after_counts, targets)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
