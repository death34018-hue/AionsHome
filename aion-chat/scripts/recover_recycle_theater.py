"""Recover missing theater rows from the intact chat.db in the Recycle Bin.

The live database is never replaced. The script freezes both databases first,
then inserts only theater rows whose primary keys do not exist in the live DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


RECYCLED_DB = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001\$RWZ4K97.db"
)
LIVE_DB = Path(r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\chat.db")
RECOVERY_DIR = Path(
    r"E:\AionRecovery_Final\recycle_chat_db_20260724_173021"
)
FROZEN_RECYCLED_DB = RECOVERY_DIR / "original_deleted_chat.db"
PRE_MERGE_DB = RECOVERY_DIR / "pre_theater_merge_chat.db"
MANIFEST_PATH = RECOVERY_DIR / "theater_merge_manifest.json"

CONVERSATION_COLUMNS = (
    "id",
    "title",
    "persona_id",
    "model",
    "created_at",
    "updated_at",
)
MESSAGE_COLUMNS = (
    "id",
    "conv_id",
    "role",
    "content",
    "created_at",
    "attachments",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze_file(source: Path, destination: Path) -> str:
    source_hash = sha256(source)
    if destination.exists():
        if sha256(destination) != source_hash:
            raise RuntimeError(f"Frozen evidence conflict: {destination}")
        return source_hash
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256(temporary) != source_hash:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Hash mismatch while freezing {source}")
    os.replace(temporary, destination)
    return source_hash


def sqlite_backup(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    source_db = sqlite3.connect(str(source), timeout=30)
    backup_db = sqlite3.connect(str(temporary))
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()
    os.replace(temporary, destination)


def rows_by_id(db: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> dict:
    selected = ", ".join(columns)
    return {
        row[0]: tuple(row)
        for row in db.execute(f"SELECT {selected} FROM {table}")
    }


def main() -> None:
    if not RECYCLED_DB.is_file():
        raise SystemExit(f"Recycled database not found: {RECYCLED_DB}")
    if not LIVE_DB.is_file():
        raise SystemExit(f"Live database not found: {LIVE_DB}")

    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    recycled_hash = freeze_file(RECYCLED_DB, FROZEN_RECYCLED_DB)
    sqlite_backup(LIVE_DB, PRE_MERGE_DB)

    source = sqlite3.connect(
        f"file:{FROZEN_RECYCLED_DB.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    live = sqlite3.connect(str(LIVE_DB), timeout=30)
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Recycled database failed quick_check")
        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed pre-merge quick_check")

        source_conversations = rows_by_id(
            source, "theater_conversations", CONVERSATION_COLUMNS
        )
        live_conversations = rows_by_id(
            live, "theater_conversations", CONVERSATION_COLUMNS
        )
        source_messages = rows_by_id(source, "theater_messages", MESSAGE_COLUMNS)
        live_messages = rows_by_id(live, "theater_messages", MESSAGE_COLUMNS)

        shared_message_conflicts = sorted(
            row_id
            for row_id in source_messages.keys() & live_messages.keys()
            if source_messages[row_id] != live_messages[row_id]
        )
        if shared_message_conflicts:
            raise RuntimeError(
                "Shared theater messages differ: "
                + ", ".join(shared_message_conflicts[:10])
            )

        missing_conversation_ids = sorted(
            source_conversations.keys() - live_conversations.keys(),
            key=lambda row_id: source_conversations[row_id][4],
        )
        missing_message_ids = sorted(
            source_messages.keys() - live_messages.keys(),
            key=lambda row_id: source_messages[row_id][4],
        )

        required_conversations = {
            source_messages[row_id][1] for row_id in missing_message_ids
        }
        unavailable_conversations = required_conversations - (
            source_conversations.keys() | live_conversations.keys()
        )
        if unavailable_conversations:
            raise RuntimeError(
                "Missing parent conversations: "
                + ", ".join(sorted(unavailable_conversations))
            )

        live.execute("BEGIN IMMEDIATE")
        live.executemany(
            "INSERT INTO theater_conversations "
            "(id, title, persona_id, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [source_conversations[row_id] for row_id in missing_conversation_ids],
        )
        live.executemany(
            "INSERT INTO theater_messages "
            "(id, conv_id, role, content, created_at, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [source_messages[row_id] for row_id in missing_message_ids],
        )
        live.commit()

        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Live database failed post-merge quick_check")

        post_conversations = rows_by_id(
            live, "theater_conversations", CONVERSATION_COLUMNS
        )
        post_messages = rows_by_id(live, "theater_messages", MESSAGE_COLUMNS)
        for row_id in missing_conversation_ids:
            if post_conversations.get(row_id) != source_conversations[row_id]:
                raise RuntimeError(f"Conversation verification failed: {row_id}")
        for row_id in missing_message_ids:
            if post_messages.get(row_id) != source_messages[row_id]:
                raise RuntimeError(f"Message verification failed: {row_id}")

        manifest = {
            "created_at": datetime.now().astimezone().isoformat(),
            "policy": "freeze first; insert missing primary keys only; no overwrite",
            "recycled_source": str(RECYCLED_DB),
            "frozen_recycled_db": str(FROZEN_RECYCLED_DB),
            "frozen_recycled_sha256": recycled_hash,
            "pre_merge_live_db": str(PRE_MERGE_DB),
            "pre_merge_live_sha256": sha256(PRE_MERGE_DB),
            "live_db": str(LIVE_DB),
            "live_post_merge_sha256": sha256(LIVE_DB),
            "inserted_conversation_ids": missing_conversation_ids,
            "inserted_message_ids": missing_message_ids,
            "inserted_message_metadata": [
                {
                    "id": row_id,
                    "conv_id": source_messages[row_id][1],
                    "role": source_messages[row_id][2],
                    "characters": len(source_messages[row_id][3] or ""),
                    "created_at": source_messages[row_id][4],
                }
                for row_id in missing_message_ids
            ],
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
                    "inserted_conversations": len(missing_conversation_ids),
                    "inserted_messages": len(missing_message_ids),
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
