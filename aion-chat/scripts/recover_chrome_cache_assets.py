"""Recover AionsHome gift images and theater artifacts from a Chrome HTTP cache snapshot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
import zlib
from pathlib import Path
from urllib.parse import urlparse


GIFT_IMAGE_RE = re.compile(r"/uploads/(gift_\d+\.(?:png|jpe?g|webp))(?:[?#]|$)", re.I)
THEATER_AUDIO_RE = re.compile(r"/api/theater/tts/audio/([^/?#]+)", re.I)
THEATER_MESSAGES_RE = re.compile(r"/api/theater/conversations/([^/?#]+)/messages(?:[?#]|$)", re.I)


def _decode_body(body: bytes, encoding: str) -> bytes:
    encoding = encoding.strip().lower()
    if body.startswith(b"\x1f\x8b") or encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _safe_write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    if path.exists():
        existing = hashlib.sha256(path.read_bytes()).hexdigest()
        return "already_present" if existing == digest else "conflict_skipped"
    temp = path.with_name(path.name + ".recovery-part")
    temp.write_bytes(data)
    temp.replace(path)
    return "written"


def _looks_like_image(name: str, data: bytes) -> bool:
    suffix = Path(name).suffix.lower()
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8") and data.rstrip().endswith(b"\xff\xd9")
    if suffix == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _looks_like_mp3(data: bytes) -> bool:
    if data.startswith(b"ID3"):
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _encoding_for(cache, key: str) -> str:
    try:
        for metadata in cache.get_metadata(key):
            if metadata is None:
                continue
            values = metadata.get_attribute("content-encoding") or []
            if values:
                return values[0]
    except Exception:
        pass
    return ""


def recover(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(args.ccl_root))
    from ccl_chromium_reader import ccl_chromium_cache as cache_module

    cache_class = cache_module.guess_cache_class(args.cache_dir)
    if cache_class is None:
        raise RuntimeError("Unsupported Chrome cache format")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    gift_stage = args.output_dir / "gift_images"
    theater_stage = args.output_dir / "theater_audio"
    api_stage = args.output_dir / "api_responses"

    manifest: dict = {
        "cache_dir": str(args.cache_dir),
        "gift_images": [],
        "theater_audio": [],
        "api_responses": [],
        "errors": [],
    }

    cache = cache_class(args.cache_dir)
    try:
        for key in cache.keys():
            gift_match = GIFT_IMAGE_RE.search(key)
            audio_match = THEATER_AUDIO_RE.search(key)
            theater_messages_match = THEATER_MESSAGES_RE.search(key)
            is_api_artifact = (
                "/api/gift/list" in key
                or "/api/gift/pending" in key
                or key.rstrip("/").endswith("/api/theater/personas")
                or theater_messages_match is not None
                or key.rstrip("/").endswith("/api/theater/conversations")
            )
            if not (gift_match or audio_match or is_api_artifact):
                continue

            try:
                bodies = cache.get_cachefile(key)
            except Exception as exc:
                manifest["errors"].append({"key": key, "error": f"{type(exc).__name__}: {exc}"})
                continue

            encoding = _encoding_for(cache, key)
            if not bodies or not any(body for body in bodies):
                target = "gift_images" if gift_match else "theater_audio" if audio_match else "api_responses"
                manifest[target].append({"key": key, "status": "metadata_only"})
                continue

            for body in bodies:
                if not body:
                    continue
                try:
                    decoded = _decode_body(body, encoding)
                except Exception as exc:
                    manifest["errors"].append(
                        {"key": key, "error": f"decode {type(exc).__name__}: {exc}"}
                    )
                    continue

                if gift_match:
                    name = gift_match.group(1)
                    if not _looks_like_image(name, decoded):
                        manifest["gift_images"].append(
                            {"key": key, "name": name, "status": "invalid_image", "bytes": len(decoded)}
                        )
                        continue
                    stage_status = _safe_write(gift_stage / name, decoded)
                    live_status = None
                    if args.uploads_dir:
                        live_status = _safe_write(args.uploads_dir / name, decoded)
                    manifest["gift_images"].append(
                        {
                            "key": key,
                            "name": name,
                            "status": "recovered",
                            "stage_status": stage_status,
                            "live_status": live_status,
                            "bytes": len(decoded),
                            "sha256": hashlib.sha256(decoded).hexdigest(),
                        }
                    )
                    continue

                if audio_match:
                    name = Path(audio_match.group(1)).name
                    if not name.lower().endswith(".mp3"):
                        name += ".mp3"
                    if not _looks_like_mp3(decoded):
                        manifest["theater_audio"].append(
                            {"key": key, "name": name, "status": "invalid_audio", "bytes": len(decoded)}
                        )
                        continue
                    stage_status = _safe_write(theater_stage / name, decoded)
                    live_status = None
                    if args.theater_tts_dir:
                        live_status = _safe_write(args.theater_tts_dir / name, decoded)
                    manifest["theater_audio"].append(
                        {
                            "key": key,
                            "name": name,
                            "status": "recovered",
                            "stage_status": stage_status,
                            "live_status": live_status,
                            "bytes": len(decoded),
                            "sha256": hashlib.sha256(decoded).hexdigest(),
                        }
                    )
                    continue

                parsed = urlparse(key.split()[-1])
                label = parsed.path.strip("/").replace("/", "_") or "root"
                if parsed.query:
                    label += "_" + hashlib.sha256(parsed.query.encode()).hexdigest()[:10]
                extension = ".json"
                try:
                    json.loads(decoded)
                except Exception:
                    extension = ".bin"
                filename = f"{label}_{hashlib.sha256(key.encode()).hexdigest()[:10]}{extension}"
                status = _safe_write(api_stage / filename, decoded)
                manifest["api_responses"].append(
                    {"key": key, "file": filename, "status": status, "bytes": len(decoded)}
                )
    finally:
        cache.close()

    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--ccl-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--uploads-dir", type=Path)
    parser.add_argument("--theater-tts-dir", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    result = recover(parse_args())
    print(
        json.dumps(
            {
                "gift_entries": len(result["gift_images"]),
                "theater_audio_entries": len(result["theater_audio"]),
                "api_entries": len(result["api_responses"]),
                "errors": len(result["errors"]),
            },
            ensure_ascii=False,
        )
    )
