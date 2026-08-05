"""Versioned client asset manifest shared by every connection route."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from threading import Lock

from config import BASE_DIR, PUBLIC_DIR


_LOCK = Lock()
_CACHED_SIGNATURE: tuple[tuple[str, int, int], ...] | None = None
_CACHED_MANIFEST: dict | None = None

_DOCUMENT_ROUTES = {
    "/": "home.html",
    "/chat": "chat.html",
    "/settings": "settings.html",
    "/capabilities": "capabilities.html",
    "/worldbook": "worldbook.html",
    "/memory": "memory.html",
    "/memory-compression": "memory-compression.html",
    "/schedule": "schedule.html",
    "/camera": "camera.html",
    "/monitor-logs": "monitor-logs.html",
    "/location": "location.html",
    "/heart-whispers": "heart-whispers.html",
    "/moments": "moments.html",
    "/diary": "diary.html",
    "/activity-logs": "activity-logs.html",
    "/reading": "reading.html",
    "/theater": "theater.html",
    "/date-theater": "date_theater.html",
    "/ghost-forest": "ghost-forest.html",
    "/gift": "gift.html",
    "/fund": "fund.html",
    "/wallpaper": "wallpaper.html",
    "/playground": "playground.html",
    "/chatroom": "chatroom.html",
    "/english-corner": "english-corner.html",
    "/doudizhu": "doudizhu.html",
    "/seeky": "seeky.html",
    "/wishes": "wishes.html",
    "/xhs-lite": "xhs-lite.html",
    "/xhs-lite-logs": "xhs-lite-logs.html",
    "/health": "health.html",
    "/hug": "hug.html",
    "/pet": "pet.html",
}


def _iter_client_assets():
    static_dir = BASE_DIR / "static"
    for route, filename in _DOCUMENT_ROUTES.items():
        path = static_dir / filename
        if path.is_file():
            yield route, path, "document"
    for path in static_dir.iterdir():
        if path.is_file() and path.suffix.lower() in {".js", ".css", ".json"}:
            yield "/static/" + path.name, path, "frontend"

    # Wallpaper videos and user-authored HTML story pages remain excluded.
    # App-owned navigable documents above are versioned explicitly by route.
    for path in PUBLIC_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".html", ".htm"}:
            continue
        relative = path.relative_to(PUBLIC_DIR).as_posix()
        if relative.startswith("wallpaper/"):
            continue
        yield "/public/" + relative, path, "visual"


def _signature(items) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (url_path, stat.st_size, stat.st_mtime_ns)
        for url_path, file_path, _category in items
        for stat in (file_path.stat(),)
    )


def get_client_asset_manifest() -> dict:
    """Return a stable, content-addressed manifest without rehashing unchanged files."""
    global _CACHED_SIGNATURE, _CACHED_MANIFEST

    items = sorted(_iter_client_assets(), key=lambda item: item[0])
    signature = _signature(items)
    with _LOCK:
        if signature == _CACHED_SIGNATURE and _CACHED_MANIFEST is not None:
            return _CACHED_MANIFEST

        files = {}
        version_seed = []
        for url_path, file_path, category in items:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            size = file_path.stat().st_size
            files[url_path] = {
                "sha256": digest,
                "size": size,
                "content_type": mimetypes.guess_type(file_path.name)[0]
                or "application/octet-stream",
                "category": category,
            }
            version_seed.append(f"{url_path}:{digest}:{size}")

        version = hashlib.sha256(
            json.dumps(version_seed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        _CACHED_SIGNATURE = signature
        _CACHED_MANIFEST = {
            "schema": 2,
            "version": version,
            "files": files,
        }
        return _CACHED_MANIFEST
