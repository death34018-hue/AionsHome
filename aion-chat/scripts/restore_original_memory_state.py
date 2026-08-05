"""Restore the two active legacy memory stores to the accident-time state.

Only the eight legacy memory-state tables proven to differ are replaced.
Chat messages, theater data, settings, and abandoned migration tables are not
modified. The live database and digest anchor are snapshotted first.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ORIGINAL_DB = Path(
    r"E:\AionRecovery_Final\recycle_chat_db_20260724_173021"
    r"\original_deleted_chat.db"
)
LIVE_DB = Path(r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\chat.db")
ORIGINAL_ANCHOR = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001"
    r"\$RPGIBFL.json"
)
LIVE_ANCHOR = Path(
    r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\digest_anchor.json"
)
SNAPSHOT_DIR = Path(
    r"E:\AionRecovery_Final\pre_original_memory_restore_20260724_2031"
)
PRE_RESTORE_DB = SNAPSHOT_DIR / "chat.db"
PRE_RESTORE_ANCHOR = SNAPSHOT_DIR / "digest_anchor.json"
FROZEN_ORIGINAL_ANCHOR = SNAPSHOT_DIR / "original_digest_anchor.json"
MANIFEST_PATH = SNAPSHOT_DIR / "memory_restore_manifest.json"
H_ORIGINAL_ANCHOR = Path(
    r"H:\Aion的数据备份存档（以防万一）"
    r"\原始数据库紧急存档_2026-07-24_173021"
    r"\digest_anchor_original_before_accident.json"
)

TARGET_TABLES = (
    "memories",
    "chatroom_memories",
    "chatroom_digest_anchors",
    "memory_compression_batches",
    "memory_compression_batch_inputs",
    "memory_compression_batch_outputs",
    "memory_compression_jobs",
    "chatroom_memory_source_edit_log",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_verified_missing_only(source: Path, destination: Path) -> None:
    source_hash = sha256(source)
    if destination.exists():
        if sha256(destination) != source_hash:
            raise RuntimeError(f"Snapshot conflict: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256(temporary) != source_hash:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Snapshot hash mismatch: {destination}")
    os.replace(temporary, destination)


def sqlite_backup(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    source_db = sqlite3.connect(str(source), timeout=30)
    backup_db = sqlite3.connect(str(temporary))
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()
    os.replace(temporary, destination)


def active_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        "aion": int(
            db.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(archive_state,'active')='active'"
            ).fetchone()[0]
        ),
        "connor": int(
            db.execute(
                "SELECT COUNT(*) FROM chatroom_memories "
                "WHERE scope='connor' "
                "AND COALESCE(archive_state,'active')='active'"
            ).fetchone()[0]
        ),
        "legacy_group_forensic": int(
            db.execute(
                "SELECT COUNT(*) FROM chatroom_memories "
                "WHERE scope='group' "
                "AND COALESCE(archive_state,'active')='active'"
            ).fetchone()[0]
        ),
        "aion_cold": int(
            db.execute(
                "SELECT COUNT(*) FROM memories WHERE archive_state='cold'"
            ).fetchone()[0]
        ),
        "connor_cold": int(
            db.execute(
                "SELECT COUNT(*) FROM chatroom_memories "
                "WHERE scope='connor' AND archive_state='cold'"
            ).fetchone()[0]
        ),
    }


def replace_table_from_source(
    source: sqlite3.Connection, live: sqlite3.Connection, table: str
) -> int:
    columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        raise RuntimeError(f"Source table is missing: {table}")
    live_columns = [
        row[1] for row in live.execute(f'PRAGMA table_info("{table}")')
    ]
    if live_columns != columns:
        raise RuntimeError(f"Schema mismatch for {table}")
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    rows = source.execute(f'SELECT {quoted_columns} FROM "{table}"').fetchall()
    live.execute(f'DELETE FROM "{table}"')
    if rows:
        live.executemany(
            f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
            rows,
        )
    return len(rows)


def main() -> None:
    for required in (ORIGINAL_DB, LIVE_DB, ORIGINAL_ANCHOR, LIVE_ANCHOR):
        if not required.is_file():
            raise SystemExit(f"Required recovery source missing: {required}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sqlite_backup(LIVE_DB, PRE_RESTORE_DB)
    copy_verified_missing_only(LIVE_ANCHOR, PRE_RESTORE_ANCHOR)
    copy_verified_missing_only(ORIGINAL_ANCHOR, FROZEN_ORIGINAL_ANCHOR)
    copy_verified_missing_only(ORIGINAL_ANCHOR, H_ORIGINAL_ANCHOR)

    source = sqlite3.connect(
        f"file:{ORIGINAL_DB.as_posix()}?mode=ro&immutable=1", uri=True
    )
    live = sqlite3.connect(str(LIVE_DB), timeout=30)
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Original database failed quick_check")
        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed pre-restore quick_check")

        source_counts = active_counts(source)
        before_counts = active_counts(live)
        if source_counts["aion"] != 590 or source_counts["connor"] != 571:
            raise RuntimeError(
                f"Unexpected source baseline: {source_counts}"
            )

        live.execute("BEGIN IMMEDIATE")
        restored_rows = {}
        for table in TARGET_TABLES:
            restored_rows[table] = replace_table_from_source(
                source, live, table
            )
        live.commit()

        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed post-restore quick_check")
        after_counts = active_counts(live)
        if after_counts != source_counts:
            raise RuntimeError(
                f"Active-count verification failed: {after_counts} != {source_counts}"
            )

        temporary_anchor = LIVE_ANCHOR.with_suffix(".json.restore-tmp")
        shutil.copy2(ORIGINAL_ANCHOR, temporary_anchor)
        if sha256(temporary_anchor) != sha256(ORIGINAL_ANCHOR):
            temporary_anchor.unlink(missing_ok=True)
            raise RuntimeError("Digest anchor hash mismatch")
        os.replace(temporary_anchor, LIVE_ANCHOR)

        manifest = {
            "created_at": datetime.now().astimezone().isoformat(),
            "policy": (
                "replace only proven legacy memory-state tables from the "
                "accident-time database; preserve all non-memory data"
            ),
            "original_db": str(ORIGINAL_DB),
            "live_db": str(LIVE_DB),
            "pre_restore_db": str(PRE_RESTORE_DB),
            "pre_restore_db_sha256": sha256(PRE_RESTORE_DB),
            "pre_restore_anchor": str(PRE_RESTORE_ANCHOR),
            "original_anchor": str(FROZEN_ORIGINAL_ANCHOR),
            "h_original_anchor": str(H_ORIGINAL_ANCHOR),
            "live_anchor_sha256": sha256(LIVE_ANCHOR),
            "original_anchor_sha256": sha256(ORIGINAL_ANCHOR),
            "before_active_counts": before_counts,
            "source_active_counts": source_counts,
            "after_active_counts": after_counts,
            "restored_rows": restored_rows,
            "preserved_abandoned_migration_tables": True,
            "quick_check": "ok",
        }
        temporary_manifest = MANIFEST_PATH.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_manifest, MANIFEST_PATH)
        print(json.dumps(manifest, ensure_ascii=False))
    except Exception:
        if live.in_transaction:
            live.rollback()
        raise
    finally:
        live.close()
        source.close()


if __name__ == "__main__":
    main()
