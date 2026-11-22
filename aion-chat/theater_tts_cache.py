"""Safe cache lifecycle helpers for theater TTS audio."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterable


_SEGMENT_SUFFIX_RE = re.compile(r"_s\d+$")
log = logging.getLogger(__name__)

_MP3_KBPS = {
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_KBPS[(False, 3)] = _MP3_KBPS[(False, 2)]
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mp3_duration(data: bytes) -> float:
    """Sum MPEG audio frame durations; 0 when the data has no frames."""
    i = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        i = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    seconds = 0.0
    while i + 4 <= len(data):
        b1, b2 = data[i + 1], data[i + 2]
        version, layer = (b1 >> 3) & 3, 4 - ((b1 >> 1) & 3)
        bitrate_index, rate_index = b2 >> 4, (b2 >> 2) & 3
        if (data[i] != 0xFF or b1 & 0xE0 != 0xE0 or version == 1 or layer == 4
                or bitrate_index in (0, 15) or rate_index == 3):
            i += 1
            continue
        mpeg1 = version == 3
        rate = _MP3_RATES[version][rate_index]
        bitrate = _MP3_KBPS[(mpeg1, layer)][bitrate_index] * 1000
        padding = (b2 >> 1) & 1
        if layer == 1:
            samples, size = 384, (12 * bitrate // rate + padding) * 4
        else:
            samples = 1152 if layer == 2 or mpeg1 else 576
            size = samples // 8 * bitrate // rate + padding
        seconds += samples / rate
        i += size
    return seconds


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

    return _delete_audio_paths(candidates, retry_attempts, retry_delay_seconds)


def delete_studio_audio_files(audio_ids: Iterable[str], cache_dir: Path) -> list[Path]:
    """Remove all chunks for exact studio recording IDs, including uncommitted chunks."""
    candidates: set[Path] = set()
    for aid in audio_ids:
        if not re.fullmatch(r'na_[a-f0-9]{32}', str(aid)):
            continue
        pattern = re.compile(rf'{re.escape(aid)}_\d+\.mp3')
        candidates.update(path for path in Path(cache_dir).glob(f'{aid}_*.mp3')
                          if pattern.fullmatch(path.name))
    return _delete_audio_paths(candidates)


def _delete_audio_paths(candidates, retry_attempts=3, retry_delay_seconds=0.05):
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
