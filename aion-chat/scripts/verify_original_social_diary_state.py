"""Verify recovered Moments, diaries, their media, and unaffected Connor memory."""

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
POST_RECOVERY_DIARY_ID = "di_connor_1784892852726"
ALLOWED_UPLOAD_CONFLICTS = {"phone_screen_latest.jpg"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rows_by_pk(db: sqlite3.Connection, table: str) -> dict[object, tuple]:
    info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    primary_keys = [(row[5], row[1]) for row in info if row[5]]
    if len(primary_keys) != 1:
        raise RuntimeError(f"Expected one primary key for {table}")
    pk_index = [row[1] for row in info].index(primary_keys[0][1])
    return {row[pk_index]: tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')}


def table_fingerprint(db: sqlite3.Connection, table: str) -> tuple[int, str]:
    rows = rows_by_pk(db, table)
    digest = hashlib.sha256()
    for key in sorted(rows, key=lambda value: str(value)):
        normalized = [
            {"blob": sha256_bytes(value), "bytes": len(value)}
            if isinstance(value, bytes)
            else value
            for value in rows[key]
        ]
        digest.update(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return len(rows), digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_source_subset(
    source: sqlite3.Connection, live: sqlite3.Connection, table: str
) -> dict[str, object]:
    source_rows = rows_by_pk(source, table)
    live_rows = rows_by_pk(live, table)
    missing = sorted(str(key) for key in source_rows.keys() - live_rows.keys())
    changed = sorted(
        str(key)
        for key in source_rows.keys() & live_rows.keys()
        if source_rows[key] != live_rows[key]
    )
    return {
        "source_count": len(source_rows),
        "live_count": len(live_rows),
        "missing_count": len(missing),
        "changed_count": len(changed),
        "exact_source_subset": not missing and not changed,
    }


def verify_directory(
    source: Path, live: Path, allowed_conflicts: set[str] | None = None
) -> dict[str, object]:
    allowed_conflicts = allowed_conflicts or set()
    missing: list[str] = []
    conflicts: list[str] = []
    source_files = [path for path in source.rglob("*") if path.is_file()]
    for source_path in source_files:
        relative = source_path.relative_to(source)
        live_path = live / relative
        relative_text = str(relative)
        if not live_path.is_file():
            missing.append(relative_text)
        elif (
            relative_text not in allowed_conflicts
            and sha256(source_path) != sha256(live_path)
        ):
            conflicts.append(relative_text)
    return {
        "source_count": len(source_files),
        "missing_count": len(missing),
        "conflict_count": len(conflicts),
        "complete": not missing and not conflicts,
    }


def main() -> int:
    source = sqlite3.connect(
        f"file:{ORIGINAL_DB.as_posix()}?mode=ro&immutable=1", uri=True
    )
    live = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            table: verify_source_subset(source, live, table)
            for table in (
                "moments",
                "moment_comments",
                "moment_reactions",
                "diary_entries",
            )
        }
        source_read_anchor = table_fingerprint(source, "moment_read_anchor")
        live_read_anchor = table_fingerprint(live, "moment_read_anchor")
        read_anchor_exact = source_read_anchor == live_read_anchor
        post_recovery_diary_preserved = (
            live.execute(
                "SELECT COUNT(*) FROM diary_entries WHERE id=?",
                (POST_RECOVERY_DIARY_ID,),
            ).fetchone()[0]
            == 1
        )
        connor_memory_exact = (
            table_fingerprint(source, "chatroom_memories")
            == table_fingerprint(live, "chatroom_memories")
            and table_fingerprint(source, "chatroom_digest_anchors")
            == table_fingerprint(live, "chatroom_digest_anchors")
        )
        uploads = verify_directory(
            RECYCLED_UPLOADS, LIVE_UPLOADS, ALLOWED_UPLOAD_CONFLICTS
        )
        diary_tts = verify_directory(RECYCLED_DIARY_TTS, LIVE_DIARY_TTS)
        quick_check = live.execute("PRAGMA quick_check").fetchone()[0]
        passed = (
            all(result["exact_source_subset"] for result in tables.values())
            and read_anchor_exact
            and post_recovery_diary_preserved
            and connor_memory_exact
            and uploads["complete"]
            and diary_tts["complete"]
            and quick_check == "ok"
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "quick_check": quick_check,
                    "tables": tables,
                    "read_anchor_exact": read_anchor_exact,
                    "post_recovery_diary_preserved": post_recovery_diary_preserved,
                    "connor_memory_exact": connor_memory_exact,
                    "uploads": uploads,
                    "diary_tts": diary_tts,
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
