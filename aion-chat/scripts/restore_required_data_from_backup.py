"""Restore selected missing AionsHome data files without overwriting live data.

This script is intentionally allowlist-based. It never copies chat.db,
digest_anchor.json, database backups, logs, or regenerable image caches.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path


SOURCE = Path(r"H:\Aion的数据备份存档（以防万一）\2026-7-24\data")
DESTINATION = Path(r"F:\MyDreamWorld\trunk\AionsHome\aion-chat\data")
MANIFEST = Path(
    r"E:\AionRecovery_Final\required_data_restore_20260724\restore_manifest.json"
)

ROOT_ALLOWLIST = {
    "cam_config.json",
    "cert.pem",
    "date_theater_config.json",
    "doudizhu_state.json",
    "fund_cache.json",
    "fund_config.json",
    "home_assistant_aliases.json",
    "home_assistant_events.json",
    "home_assistant_events.jsonl",
    "home_assistant_mcp.json",
    "key.pem",
    "location_config.json",
    "location_status.json",
    "mcp_servers.json",
    "wallpaper_config.json",
    "xhs_lite_config.json",
    "xhs_lite_runs.jsonl",
    "鬣狗动态监督.md",
}

# TTS is expensive to recreate; the other two directories contain useful live
# state/history. All are restored on a missing-only basis.
DIRECTORY_ALLOWLIST = {
    "activity_logs",
    "phone_screens",
    "tts_cache",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def selected_files() -> list[Path]:
    files: list[Path] = []
    for name in sorted(ROOT_ALLOWLIST):
        path = SOURCE / name
        if path.is_file():
            files.append(path)
    for dirname in sorted(DIRECTORY_ALLOWLIST):
        root = SOURCE / dirname
        if root.is_dir():
            files.extend(sorted(path for path in root.rglob("*") if path.is_file()))
    return files


def main() -> None:
    if not SOURCE.is_dir():
        raise SystemExit(f"Backup data directory does not exist: {SOURCE}")
    if not DESTINATION.is_dir():
        raise SystemExit(f"Live data directory does not exist: {DESTINATION}")

    restored: list[dict[str, object]] = []
    skipped_existing: list[str] = []
    missing_at_source: list[str] = []

    for name in sorted(ROOT_ALLOWLIST):
        if not (SOURCE / name).is_file():
            missing_at_source.append(name)

    for source_path in selected_files():
        relative = source_path.relative_to(SOURCE)
        destination_path = DESTINATION / relative
        if destination_path.exists():
            skipped_existing.append(str(relative))
            continue

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination_path.with_name(
            f".{destination_path.name}.restore-{os.getpid()}.tmp"
        )
        shutil.copy2(source_path, temporary_path)
        source_hash = sha256(source_path)
        copied_hash = sha256(temporary_path)
        if copied_hash != source_hash:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"Hash mismatch while restoring {relative}")
        os.replace(temporary_path, destination_path)
        restored.append(
            {
                "relative_path": str(relative),
                "size": destination_path.stat().st_size,
                "sha256": copied_hash,
            }
        )

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "source": str(SOURCE),
        "destination": str(DESTINATION),
        "policy": "allowlist, missing-only, hash-verified, never overwrite",
        "root_allowlist": sorted(ROOT_ALLOWLIST),
        "directory_allowlist": sorted(DIRECTORY_ALLOWLIST),
        "restored_count": len(restored),
        "restored_bytes": sum(int(item["size"]) for item in restored),
        "skipped_existing_count": len(skipped_existing),
        "missing_at_source": missing_at_source,
        "restored": restored,
        "skipped_existing": skipped_existing,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = MANIFEST.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary_manifest, MANIFEST)

    print(
        json.dumps(
            {
                "restored_count": manifest["restored_count"],
                "restored_bytes": manifest["restored_bytes"],
                "skipped_existing_count": manifest["skipped_existing_count"],
                "missing_at_source": manifest["missing_at_source"],
                "manifest": str(MANIFEST),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
