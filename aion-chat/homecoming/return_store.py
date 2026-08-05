"""Quarantine persistence for verified Homecoming return packages."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .return_contracts import InvalidReturnPackage, VerifiedPackage


async def ensure_return_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_return_packages (
            package_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            epoch_id TEXT NOT NULL,
            base_snapshot_id TEXT NOT NULL,
            first_device_seq INTEGER NOT NULL,
            highest_device_seq INTEGER NOT NULL,
            operation_count INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            signature_b64 TEXT NOT NULL,
            quarantine_path TEXT NOT NULL,
            state TEXT NOT NULL,
            received_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(device_id, highest_device_seq),
            FOREIGN KEY (device_id) REFERENCES homecoming_devices(device_id)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_return_operations (
            op_id TEXT PRIMARY KEY,
            package_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            device_seq INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            base_revision TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            state TEXT NOT NULL,
            UNIQUE(device_id, device_seq),
            FOREIGN KEY (package_id) REFERENCES homecoming_return_packages(package_id)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_import_sessions (
            import_session_id TEXT PRIMARY KEY,
            package_id TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            main_state_sha256 TEXT NOT NULL DEFAULT '',
            counts_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL,
            accepted_highest_device_seq INTEGER NOT NULL DEFAULT 0,
            result_summary_sha256 TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (package_id) REFERENCES homecoming_return_packages(package_id)
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(homecoming_import_sessions)")
    session_columns = {row[1] for row in await cursor.fetchall()}
    if "main_state_sha256" not in session_columns:
        await db.execute(
            "ALTER TABLE homecoming_import_sessions ADD COLUMN "
            "main_state_sha256 TEXT NOT NULL DEFAULT ''"
        )
    if "counts_json" not in session_columns:
        await db.execute(
            "ALTER TABLE homecoming_import_sessions ADD COLUMN "
            "counts_json TEXT NOT NULL DEFAULT '{}'"
        )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_import_results (
            import_session_id TEXT NOT NULL,
            op_id TEXT NOT NULL,
            device_seq INTEGER NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            result_json TEXT NOT NULL,
            apply_state TEXT NOT NULL DEFAULT 'pending',
            applied_at REAL,
            created_at REAL NOT NULL,
            PRIMARY KEY (import_session_id, op_id),
            FOREIGN KEY (import_session_id)
                REFERENCES homecoming_import_sessions(import_session_id)
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(homecoming_import_results)")
    result_columns = {row[1] for row in await cursor.fetchall()}
    if "apply_state" not in result_columns:
        await db.execute(
            "ALTER TABLE homecoming_import_results ADD COLUMN "
            "apply_state TEXT NOT NULL DEFAULT 'pending'"
        )
    if "applied_at" not in result_columns:
        await db.execute(
            "ALTER TABLE homecoming_import_results ADD COLUMN applied_at REAL"
        )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS homecoming_summary_coverage (
            owner_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            epoch_id TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            import_session_id TEXT NOT NULL,
            verified_at REAL NOT NULL,
            PRIMARY KEY (owner_id, message_id)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_homecoming_summary_coverage_checkpoint "
        "ON homecoming_summary_coverage(checkpoint_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_homecoming_summary_coverage_import "
        "ON homecoming_summary_coverage(import_session_id)"
    )


async def store_verified_package(
    db,
    package: VerifiedPackage,
    raw_gzip: bytes,
    root: Path,
    now: float,
) -> dict:
    await ensure_return_tables(db)
    existing = await _package_row(db, package.package_id)
    if existing is not None:
        if existing["payload_sha256"] != package.payload_sha256:
            raise InvalidReturnPackage("return package id is already bound")
        return _status(existing)

    cursor = await db.execute(
        "SELECT COALESCE(MAX(highest_device_seq),0) "
        "FROM homecoming_return_packages WHERE device_id=?",
        (package.device_id,),
    )
    previous_highest = int((await cursor.fetchone())[0])
    expected_first = previous_highest + 1
    if package.first_device_seq != expected_first:
        raise InvalidReturnPackage(
            f"return sequence gap: expected {expected_first}"
        )

    target = _quarantine_target(root, package.payload_sha256)
    _write_once(target, raw_gzip)
    await db.execute(
        "INSERT INTO homecoming_return_packages "
        "(package_id,device_id,epoch_id,base_snapshot_id,first_device_seq,"
        "highest_device_seq,operation_count,payload_sha256,signature_b64,"
        "quarantine_path,state,received_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            package.package_id,
            package.device_id,
            package.epoch_id,
            package.base_snapshot_id,
            package.first_device_seq,
            package.highest_device_seq,
            package.operation_count,
            package.payload_sha256,
            package.signature_b64,
            str(target),
            "received",
            float(now),
            float(now),
        ),
    )
    for operation in package.operations:
        await db.execute(
            "INSERT INTO homecoming_return_operations "
            "(op_id,package_id,device_id,device_seq,entity_type,entity_id,"
            "action,base_revision,payload_json,created_at,state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation.op_id,
                package.package_id,
                package.device_id,
                operation.device_seq,
                operation.entity_type,
                operation.entity_id,
                operation.action,
                operation.base_revision,
                json.dumps(
                    operation.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                operation.created_at,
                "received",
            ),
        )
    row = await _package_row(db, package.package_id)
    if row is None:
        raise RuntimeError("return package was not persisted")
    return _status(row)


async def get_return_package_status(db, package_id: str) -> dict | None:
    await ensure_return_tables(db)
    row = await _package_row(db, package_id)
    if row is None:
        return None
    status = _status(row)
    cursor = await db.execute(
        "SELECT import_session_id,state,accepted_highest_device_seq,"
        "result_summary_sha256,counts_json "
        "FROM homecoming_import_sessions WHERE package_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (package_id,),
    )
    session = await cursor.fetchone()
    if session is not None:
        status.update(
            {
                "import_session_id": session[0],
                "import_state": session[1],
                "accepted_highest_device_seq": session[2],
                "result_summary_sha256": session[3],
                "counts": json.loads(session[4] or "{}"),
                "complete": session[1] == "confirmed",
            }
        )
    return status


async def latest_import_plan_id(db, package_id: str) -> str | None:
    await ensure_return_tables(db)
    cursor = await db.execute(
        "SELECT import_session_id FROM homecoming_import_sessions "
        "WHERE package_id=? AND state IN ('planned','applying','confirmed') "
        "ORDER BY created_at DESC LIMIT 1",
        (package_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row[0])


async def _package_row(db, package_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT package_id,device_id,epoch_id,first_device_seq,"
        "highest_device_seq,operation_count,payload_sha256,state,"
        "received_at,updated_at FROM homecoming_return_packages "
        "WHERE package_id=?",
        (package_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "package_id": row[0],
        "device_id": row[1],
        "epoch_id": row[2],
        "first_device_seq": row[3],
        "highest_device_seq": row[4],
        "operation_count": row[5],
        "payload_sha256": row[6],
        "state": row[7],
        "received_at": row[8],
        "updated_at": row[9],
    }


def _status(row: dict) -> dict:
    return {
        "package_id": row["package_id"],
        "device_id": row["device_id"],
        "epoch_id": row["epoch_id"],
        "first_device_seq": row["first_device_seq"],
        "highest_device_seq": row["highest_device_seq"],
        "operation_count": row["operation_count"],
        "payload_sha256": row["payload_sha256"],
        "state": row["state"],
    }


def _quarantine_target(root: Path, digest: str) -> Path:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / f"{digest}.json.gz").resolve()
    if target.parent != root:
        raise InvalidReturnPackage("invalid quarantine path")
    return target


def _write_once(target: Path, raw: bytes) -> None:
    if target.exists():
        if target.read_bytes() != raw:
            raise InvalidReturnPackage("quarantine blob hash collision")
        return
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
