"""Verify that live legacy memory state exactly matches the accident-time source."""

from __future__ import annotations

import hashlib
import json
import sqlite3
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


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_value(value: object) -> object:
    if isinstance(value, bytes):
        return {
            "blob_sha256": hashlib.sha256(value).hexdigest(),
            "bytes": len(value),
        }
    return value


def table_fingerprint(db: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    primary_keys = [
        (row[5], row[1])
        for row in db.execute(f'PRAGMA table_info("{table}")')
        if row[5]
    ]
    order_columns = [name for _, name in sorted(primary_keys)] or columns
    order_sql = ", ".join(f'"{name}"' for name in order_columns)
    digest = hashlib.sha256()
    count = 0
    for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}'):
        digest.update(
            json.dumps(
                [normalized_value(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def main() -> int:
    source = sqlite3.connect(
        f"file:{ORIGINAL_DB.as_posix()}?mode=ro&immutable=1", uri=True
    )
    live = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    try:
        table_results = {}
        for table in TARGET_TABLES:
            source_fingerprint = table_fingerprint(source, table)
            live_fingerprint = table_fingerprint(live, table)
            table_results[table] = {
                "source_count": source_fingerprint[0],
                "live_count": live_fingerprint[0],
                "source_sha256": source_fingerprint[1],
                "live_sha256": live_fingerprint[1],
                "exact": source_fingerprint == live_fingerprint,
            }

        active_counts = {
            "source_aion": source.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(archive_state,'active')='active'"
            ).fetchone()[0],
            "live_aion": live.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(archive_state,'active')='active'"
            ).fetchone()[0],
            "source_connor": source.execute(
                "SELECT COUNT(*) FROM chatroom_memories "
                "WHERE scope='connor' "
                "AND COALESCE(archive_state,'active')='active'"
            ).fetchone()[0],
            "live_connor": live.execute(
                "SELECT COUNT(*) FROM chatroom_memories "
                "WHERE scope='connor' "
                "AND COALESCE(archive_state,'active')='active'"
            ).fetchone()[0],
        }
        anchor_exact = file_hash(ORIGINAL_ANCHOR) == file_hash(LIVE_ANCHOR)
        quick_check = live.execute("PRAGMA quick_check").fetchone()[0]
        passed = (
            all(result["exact"] for result in table_results.values())
            and active_counts["source_aion"] == active_counts["live_aion"]
            and active_counts["source_connor"] == active_counts["live_connor"]
            and anchor_exact
            and quick_check == "ok"
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "quick_check": quick_check,
                    "active_counts": active_counts,
                    "anchor_exact": anchor_exact,
                    "tables": table_results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if passed else 1
    finally:
        live.close()
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
