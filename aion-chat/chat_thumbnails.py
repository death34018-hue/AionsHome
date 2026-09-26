"""On-demand chat previews. Original attachments and their URLs stay unchanged."""
import hashlib
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

_LOCKS = [threading.Lock() for _ in range(8)]


def create_router(roots: dict[str, Path], cache_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get('/api/chat-media/thumbnail')
    def thumbnail(request: Request, src: str = Query(max_length=2048)):
        # Sync route: Pillow/file work runs in FastAPI's thread pool, not the event loop.
        parsed = urlsplit(src)
        path = unquote(parsed.path)
        if parsed.scheme or parsed.netloc or not path.startswith('/') or '\\' in path:
            raise HTTPException(400, 'Invalid image path')
        if path.startswith('/public/wallpaper/'):
            raise HTTPException(404, 'Image not found')
        source = None
        for prefix, root in sorted(roots.items(), key=lambda item: -len(item[0])):
            if path.startswith(prefix):
                root = root.resolve()
                candidate = (root / path[len(prefix):]).resolve()
                if not candidate.is_relative_to(root):
                    raise HTTPException(404, 'Image not found')
                if prefix == '/public/' and candidate.is_relative_to((root / 'wallpaper').resolve()):
                    raise HTTPException(404, 'Image not found')
                source = candidate
                break
        if source is None or not source.is_file() or source.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
            raise HTTPException(404, 'Image not found')
        stat = source.stat()
        key = hashlib.sha256(f'v1:{source}:{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()
        target = cache_dir / (key + '.webp')
        headers = {'Cache-Control': 'private, max-age=3600', 'ETag': f'"{key}"'}
        if request.headers.get('if-none-match') == headers['ETag']:
            return Response(status_code=304, headers=headers)
        with _LOCKS[int(key[:2], 16) % len(_LOCKS)]:
            if not target.is_file():
                try:
                    with Image.open(source) as original:
                        original.draft('RGB', (960, 960))
                        preview = ImageOps.exif_transpose(original)
                        preview.thumbnail((480, 480), Image.Resampling.LANCZOS)
                        if preview.mode not in ('RGB', 'RGBA'):
                            preview = preview.convert('RGBA' if 'transparency' in preview.info else 'RGB')
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        temp = target.with_suffix('.tmp')
                        preview.save(temp, 'WEBP', quality=72)
                        temp.replace(target)
                except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                    raise HTTPException(415, 'Unable to preview this image') from None
        return FileResponse(target, media_type='image/webp', headers=headers)

    return router
