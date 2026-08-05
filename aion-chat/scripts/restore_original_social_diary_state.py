"""Merge accident-time Moments and diaries while preserving newer live rows."""

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
RECYCLED_UPLOADS = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001"
    r"\$RAH19XC"
)
LIVE_UPLOADS = Path(r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\uploads")
RECYCLED_DIARY_TTS = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001"
    r"\$RB3IDL5"
)
LIVE_DIARY_TTS = Path(
    r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\diary_tts_cache"
)
RECOVERY_DIR = Path(
    r"E:\AionRecovery_Final\pre_social_diary_restore_20260724_2042"
)
PRE_RESTORE_DB = RECOVERY_DIR / "chat.db"
FROZEN_UPLOADS = RECOVERY_DIR / "recovered_uploads"
FROZEN_DIARY_TTS = RECOVERY_DIR / "recovered_diary_tts"
MANIFEST_PATH = RECOVERY_DIR / "social_diary_restore_manifest.json"

MERGE_TABLES = (
    "moments",
    "moment_comments",
    "moment_reactions",
    "diary_entries",
)
POST_RECOVERY_DIARY_ID = "di_connor_1784892852726"
ALLOWED_UPLOAD_CONFLICTS = {"phone_screen_latest.jpg"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def copy_verified(source: Path, destination: Path, digest: str) -> str:
    if destination.exists():
        return "already_present" if sha256(destination) == digest else "conflict"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Hash mismatch while copying {source}")
    os.replace(temporary, destination)
    return "written"


def copy_missing_directory(
    source_root: Path,
    live_root: Path,
    frozen_root: Path,
    allowed_conflicts: set[str] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    allowed_conflicts = allowed_conflicts or set()
    restored: list[dict[str, object]] = []
    conflicts: list[str] = []
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = source.relative_to(source_root)
        relative_text = str(relative)
        live_path = live_root / relative
        digest = sha256(source)
        if live_path.exists():
            if sha256(live_path) != digest and relative_text not in allowed_conflicts:
                conflicts.append(relative_text)
            continue
        frozen_status = copy_verified(source, frozen_root / relative, digest)
        live_status = copy_verified(source, live_path, digest)
        if frozen_status == "conflict" or live_status == "conflict":
            raise RuntimeError(f"Unexpected copy conflict: {relative}")
        restored.append(
            {
                "relative_path": relative_text,
                "bytes": source.stat().st_size,
                "sha256": digest,
                "frozen_status": frozen_status,
                "live_status": live_status,
            }
        )
    if conflicts:
        raise RuntimeError("Unapproved media conflicts: " + ", ".join(conflicts))
    return restored, sorted(allowed_conflicts)


def merge_source_rows(
    source: sqlite3.Connection, live: sqlite3.Connection, table: str
) -> int:
    info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = [row[1] for row in info]
    primary_keys = [(row[5], row[1]) for row in info if row[5]]
    if len(primary_keys) != 1:
        raise RuntimeError(f"Expected one primary key for {table}")
    pk_name = primary_keys[0][1]
    pk_index = columns.index(pk_name)
    live_columns = [
        row[1] for row in live.execute(f'PRAGMA table_info("{table}")')
    ]
    if columns != live_columns:
        raise RuntimeError(f"Schema mismatch for {table}")

    live_rows = {
        row[pk_index]: tuple(row)
        for row in live.execute(f'SELECT * FROM "{table}"')
    }
    missing_rows = []
    for row in source.execute(f'SELECT * FROM "{table}"'):
        row_tuple = tuple(row)
        key = row_tuple[pk_index]
        if key in live_rows:
            if live_rows[key] != row_tuple:
                raise RuntimeError(f"Existing row differs in {table}: {key}")
        else:
            missing_rows.append(row_tuple)

    if missing_rows:
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        live.executemany(
            f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
            missing_rows,
        )
    return len(missing_rows)


def fingerprint_rows(db: sqlite3.Connection, table: str) -> str:
    info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = [row[1] for row in info]
    primary_keys = [(row[5], row[1]) for row in info if row[5]]
    order_columns = [name for _, name in sorted(primary_keys)] or columns
    order_sql = ", ".join(f'"{name}"' for name in order_columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}'):
        values = [
            {"blob": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
            if isinstance(value, bytes)
            else value
            for value in row
        ]
        digest.update(
            json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def main() -> None:
    required = (
        ORIGINAL_DB,
        LIVE_DB,
        RECYCLED_UPLOADS,
        RECYCLED_DIARY_TTS,
    )
    for path in required:
        if not path.exists():
            raise SystemExit(f"Required recovery source missing: {path}")

    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    sqlite_backup(LIVE_DB, PRE_RESTORE_DB)

    source = sqlite3.connect(
        f"file:{ORIGINAL_DB.as_posix()}?mode=ro&immutable=1", uri=True
    )
    live = sqlite3.connect(str(LIVE_DB), timeout=30)
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Original database failed quick_check")
        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed pre-restore quick_check")
        if (
            live.execute(
                "SELECT COUNT(*) FROM diary_entries WHERE id=?",
                (POST_RECOVERY_DIARY_ID,),
            ).fetchone()[0]
            != 1
        ):
            raise RuntimeError("The 19:34 Connor diary is not present")

        connor_memory_before = fingerprint_rows(live, "chatroom_memories")
        connor_anchor_before = fingerprint_rows(live, "chatroom_digest_anchors")

        live.execute("BEGIN IMMEDIATE")
        inserted = {
            table: merge_source_rows(source, live, table)
            for table in MERGE_TABLES
        }
        source_read_anchor = source.execute(
            "SELECT id, last_read_at FROM moment_read_anchor"
        ).fetchall()
        live.execute("DELETE FROM moment_read_anchor")
        live.executemany(
            "INSERT INTO moment_read_anchor (id, last_read_at) VALUES (?, ?)",
            source_read_anchor,
        )
        live.commit()

        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed post-restore quick_check")
        if fingerprint_rows(live, "chatroom_memories") != connor_memory_before:
            raise RuntimeError("Connor memories changed during social recovery")
        if fingerprint_rows(live, "chatroom_digest_anchors") != connor_anchor_before:
            raise RuntimeError("Connor anchor changed during social recovery")
        if (
            live.execute(
                "SELECT COUNT(*) FROM diary_entries WHERE id=?",
                (POST_RECOVERY_DIARY_ID,),
            ).fetchone()[0]
            != 1
        ):
            raise RuntimeError("The 19:34 Connor diary was not preserved")

        restored_uploads, allowed_upload_conflicts = copy_missing_directory(
            RECYCLED_UPLOADS,
            LIVE_UPLOADS,
            FROZEN_UPLOADS,
            ALLOWED_UPLOAD_CONFLICTS,
        )
        restored_diary_tts, _ = copy_missing_directory(
            RECYCLED_DIARY_TTS,
            LIVE_DIARY_TTS,
            FROZEN_DIARY_TTS,
        )

        manifest = {
            "created_at": datetime.now().astimezone().isoformat(),
            "policy": (
                "merge source rows by primary key; preserve all newer live rows; "
                "never overwrite media"
            ),
            "original_db": str(ORIGINAL_DB),
            "pre_restore_db": str(PRE_RESTORE_DB),
            "pre_restore_db_sha256": sha256(PRE_RESTORE_DB),
            "inserted_rows": inserted,
            "post_recovery_diary_preserved": POST_RECOVERY_DIARY_ID,
            "restored_upload_count": len(restored_uploads),
            "restored_upload_bytes": sum(
                int(item["bytes"]) for item in restored_uploads
            ),
            "restored_diary_tts_count": len(restored_diary_tts),
            "restored_diary_tts_bytes": sum(
                int(item["bytes"]) for item in restored_diary_tts
            ),
            "allowed_upload_conflicts_preserved": allowed_upload_conflicts,
            "restored_uploads": restored_uploads,
            "restored_diary_tts": restored_diary_tts,
            "connor_memory_unchanged": True,
            "connor_anchor_unchanged": True,
            "quick_check": "ok",
        }
        temporary_manifest = MANIFEST_PATH.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_manifest, MANIFEST_PATH)
        print(
            json.dumps(
                {
                    "inserted_rows": inserted,
                    "restored_upload_count": len(restored_uploads),
                    "restored_diary_tts_count": len(restored_diary_tts),
                    "connor_memory_unchanged": True,
                    "quick_check": "ok",
                    "manifest": str(MANIFEST_PATH),
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        if live.in_transaction:
            live.rollback()
        raise
    finally:
        live.close()
        source.close()


if __name__ == "__main__":
    main()
