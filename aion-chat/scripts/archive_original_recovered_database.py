"""Create verified archival copies of the intact pre-accident chat database."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path


RECYCLE_SOURCE = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001\$RWZ4K97.db"
)
EVIDENCE_COPY = Path(
    r"E:\AionRecovery_Final\recycle_chat_db_20260724_173021"
    r"\original_deleted_chat.db"
)
H_ARCHIVE_DIR = Path(
    r"H:\Aion的数据备份存档（以防万一）"
    r"\原始数据库紧急存档_2026-07-24_173021"
)
H_ARCHIVE_COPY = H_ARCHIVE_DIR / "chat_original_before_accident.db"
H_MANIFEST = H_ARCHIVE_DIR / "archive_manifest.json"
H_CHECKSUM = H_ARCHIVE_DIR / "SHA256.txt"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_verified_missing_only(source: Path, destination: Path, digest: str) -> str:
    if destination.exists():
        if sha256(destination) != digest:
            raise RuntimeError(f"Archive conflict: {destination}")
        return "already_present"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Archive hash mismatch: {destination}")
    os.replace(temporary, destination)
    return "written"


def quick_check(path: Path) -> str:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True, timeout=30
    )
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()


def main() -> None:
    if not RECYCLE_SOURCE.is_file():
        raise SystemExit(f"Recycle Bin source missing: {RECYCLE_SOURCE}")
    if not EVIDENCE_COPY.is_file():
        raise SystemExit(f"Evidence copy missing: {EVIDENCE_COPY}")

    source_digest = sha256(RECYCLE_SOURCE)
    if sha256(EVIDENCE_COPY) != source_digest:
        raise RuntimeError("E evidence copy does not match Recycle Bin source")
    if quick_check(RECYCLE_SOURCE) != "ok" or quick_check(EVIDENCE_COPY) != "ok":
        raise RuntimeError("Source or E evidence copy failed SQLite quick_check")

    archive_status = copy_verified_missing_only(
        EVIDENCE_COPY, H_ARCHIVE_COPY, source_digest
    )
    if quick_check(H_ARCHIVE_COPY) != "ok":
        raise RuntimeError("H archive copy failed SQLite quick_check")

    # The archives are evidence copies, not runtime databases.
    os.chmod(EVIDENCE_COPY, stat.S_IREAD)
    os.chmod(H_ARCHIVE_COPY, stat.S_IREAD)

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "description": "Intact AionsHome chat.db deleted at 2026-07-24 17:30:21 +08:00",
        "original_path": r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\chat.db",
        "recycle_bin_source": str(RECYCLE_SOURCE),
        "evidence_copy": str(EVIDENCE_COPY),
        "h_archive_copy": str(H_ARCHIVE_COPY),
        "bytes": H_ARCHIVE_COPY.stat().st_size,
        "sha256": source_digest,
        "sqlite_quick_check": "ok",
        "archive_status": archive_status,
        "warning": "Evidence archive only. Do not replace the live chat.db directly.",
    }
    H_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    H_CHECKSUM.write_text(
        f"{source_digest}  {H_ARCHIVE_COPY.name}\n", encoding="ascii"
    )
    print(
        json.dumps(
            {
                "archive_status": archive_status,
                "bytes": manifest["bytes"],
                "sha256": source_digest,
                "quick_check": "ok",
                "evidence_copy": str(EVIDENCE_COPY),
                "h_archive_copy": str(H_ARCHIVE_COPY),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
