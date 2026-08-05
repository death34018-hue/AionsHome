"""Freeze and restore missing theater MP3 files from the Recycle Bin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path


RECYCLED_AUDIO_DIR = Path(
    r"F:\$Recycle.Bin\S-1-5-21-1585213737-251005820-159614300-1001\$RR70GQI"
)
LIVE_AUDIO_DIR = Path(
    r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data\theater_tts_cache"
)
RECOVERY_DIR = Path(
    r"E:\AionRecovery_Final\recycle_chat_db_20260724_173021"
    r"\recovered_theater_audio"
)
MANIFEST_PATH = RECOVERY_DIR.parent / "theater_audio_manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def looks_like_mp3(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(3)
    return header == b"ID3" or (
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
    )


def copy_missing_verified(source: Path, destination: Path, digest: str) -> str:
    if destination.exists():
        if sha256(destination) != digest:
            raise RuntimeError(f"Existing file conflicts with source: {destination}")
        return "already_present"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Hash mismatch while copying {source}")
    os.replace(temporary, destination)
    return "written"


def main() -> None:
    if not RECYCLED_AUDIO_DIR.is_dir():
        raise SystemExit(f"Recycled theater audio directory missing: {RECYCLED_AUDIO_DIR}")
    LIVE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)

    recovered: list[dict[str, object]] = []
    for source in sorted(RECYCLED_AUDIO_DIR.glob("*.mp3")):
        live_path = LIVE_AUDIO_DIR / source.name
        if live_path.exists():
            continue
        if not looks_like_mp3(source):
            raise RuntimeError(f"Invalid MP3 header: {source}")
        digest = sha256(source)
        frozen_status = copy_missing_verified(
            source, RECOVERY_DIR / source.name, digest
        )
        live_status = copy_missing_verified(source, live_path, digest)
        recovered.append(
            {
                "name": source.name,
                "bytes": source.stat().st_size,
                "sha256": digest,
                "frozen_status": frozen_status,
                "live_status": live_status,
            }
        )

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "policy": "valid MP3, hash verified, missing-only, never overwrite",
        "recycled_source": str(RECYCLED_AUDIO_DIR),
        "frozen_directory": str(RECOVERY_DIR),
        "live_directory": str(LIVE_AUDIO_DIR),
        "recovered_count": len(recovered),
        "recovered_bytes": sum(int(item["bytes"]) for item in recovered),
        "recovered": recovered,
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
                "recovered_count": manifest["recovered_count"],
                "recovered_bytes": manifest["recovered_bytes"],
                "manifest": str(MANIFEST_PATH),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
