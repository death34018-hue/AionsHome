"""Safe cache lifecycle helpers for theater TTS audio."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterable


_SEGMENT_SUFFIX_RE = re.compile(r"_s\d+$")
log = logging.getLogger(__name__)


def _safe_message_id(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "", str(value or ""))


def message_id_from_audio_path(path: Path) -> str | None:
    path = Path(path)
    if path.suffix.lower() != ".mp3":
        return None
    stem = path.stem
    message_id = _SEGMENT_SUFFIX_RE.sub("", stem)
    return message_id or None


def list_message_audio_segments(message_id: str, cache_dir: Path) -> list[tuple[int, Path]]:
    safe_id = _safe_message_id(message_id)
    if not safe_id:
        return []
    cache_dir = Path(cache_dir)
    segment_name = re.compile(rf"^{re.escape(safe_id)}_s(\d+)\.mp3$")
    segments: list[tuple[int, Path]] = []
    for path in cache_dir.glob(f"{safe_id}_s*.mp3"):
        if not path.is_file():
            continue
        match = segment_name.fullmatch(path.name)
        if match:
            segments.append((int(match.group(1)), path))
    return sorted(segments, key=lambda item: item[0])


def delete_message_audio_files(
    message_ids: Iterable[str],
    cache_dir: Path,
    *,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 0.05,
) -> list[Path]:
    cache_dir = Path(cache_dir)
    candidates: set[Path] = set()
    for message_id in message_ids:
        safe_id = _safe_message_id(message_id)
        if not safe_id:
            continue
        candidates.add(cache_dir / f"{safe_id}.mp3")
        candidates.update(path for _, path in list_message_audio_segments(safe_id, cache_dir))

    deleted: list[Path] = []
    for path in sorted(candidates, key=lambda item: item.name):
        if not path.is_file():
            continue
        attempts = max(1, int(retry_attempts))
        for attempt in range(attempts):
            try:
                path.unlink(missing_ok=True)
                deleted.append(path)
                break
            except OSError as exc:
                if attempt + 1 >= attempts:
                    log.warning("Theater TTS cache deletion failed for %s: %s", path, exc)
                    break
                time.sleep(max(0.0, float(retry_delay_seconds)))
    return deleted


def find_orphan_audio_files(
    valid_message_ids: Iterable[str],
    cache_dir: Path,
    *,
    min_age_seconds: float,
    now: float | None = None,
) -> list[Path]:
    cache_dir = Path(cache_dir)
    valid_ids = {_safe_message_id(message_id) for message_id in valid_message_ids}
    valid_ids.discard("")
    cutoff_now = time.time() if now is None else float(now)
    minimum_age = max(0.0, float(min_age_seconds))

    orphans: list[Path] = []
    for path in cache_dir.glob("*.mp3"):
        if not path.is_file():
            continue
        message_id = message_id_from_audio_path(path)
        if not message_id or message_id in valid_ids:
            continue
        try:
            age = cutoff_now - path.stat().st_mtime
        except OSError:
            continue
        if age >= minimum_age:
            orphans.append(path)
    return sorted(orphans, key=lambda item: item.name)
