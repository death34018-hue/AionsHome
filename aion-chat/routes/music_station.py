"""HTTP API for the standalone 点歌台."""

from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from music import get_audio_url
from music_station import (
    MAX_UPLOAD_BYTES,
    get_music_station_store,
    validate_audio_upload,
)


router = APIRouter(prefix="/api/music-station", tags=["music-station"])
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def local_audio_response(path: Path, mime_type: str, range_header: str | None):
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if not range_header:
        return FileResponse(path, media_type=mime_type, headers=headers)
    match = _RANGE_RE.match(range_header.strip())
    if not match or size <= 0:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start_text, end_text = match.groups()
    if not start_text:
        length = int(end_text or 0)
        if length <= 0:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_text)
        end = min(size - 1, int(end_text)) if end_text else size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    with path.open("rb") as handle:
        handle.seek(start)
        body = handle.read(end - start + 1)
    headers.update({
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(len(body)),
    })
    return Response(body, status_code=206, media_type=mime_type, headers=headers)


class TrimBody(BaseModel):
    start_ms: int = 0
    end_ms: int = 0


class MetadataBody(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    lyrics_lrc: str | None = None


class PlaylistBody(BaseModel):
    name: str


class TrackIdsBody(BaseModel):
    track_ids: list[str]


@router.get("/tracks")
async def list_tracks(playlist_id: str | None = None):
    try:
        return {"tracks": await get_music_station_store().list_tracks(playlist_id)}
    except KeyError:
        raise HTTPException(404, "歌单不存在")


@router.delete("/tracks")
async def delete_tracks(body: TrackIdsBody):
    try:
        return await get_music_station_store().delete_tracks(body.track_ids)
    except KeyError:
        raise HTTPException(404, "歌曲不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/playlists")
async def list_playlists():
    return {"playlists": await get_music_station_store().list_playlists()}


@router.post("/playlists")
async def create_playlist(body: PlaylistBody):
    try:
        return await get_music_station_store().create_playlist(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/playlists/{playlist_id}")
async def rename_playlist(playlist_id: str, body: PlaylistBody):
    try:
        return await get_music_station_store().rename_playlist(playlist_id, body.name)
    except KeyError:
        raise HTTPException(404, "歌单不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(playlist_id: str):
    try:
        await get_music_station_store().delete_playlist(playlist_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(404, "歌单不存在")


@router.post("/playlists/{playlist_id}/tracks")
async def add_playlist_tracks(playlist_id: str, body: TrackIdsBody):
    try:
        return {"added": await get_music_station_store().add_tracks_to_playlist(
            playlist_id, body.track_ids
        )}
    except KeyError:
        raise HTTPException(404, "歌单或歌曲不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/playlists/{playlist_id}/tracks")
async def remove_playlist_tracks(playlist_id: str, body: TrackIdsBody):
    try:
        return {"removed": await get_music_station_store().remove_tracks_from_playlist(
            playlist_id, body.track_ids
        )}
    except KeyError:
        raise HTTPException(404, "歌单不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/tracks/{track_id}/trim")
async def update_trim(track_id: str, body: TrimBody):
    try:
        return await get_music_station_store().update_trim(track_id, body.start_ms, body.end_ms)
    except KeyError:
        raise HTTPException(404, "歌曲不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/tracks/{track_id}")
async def update_track(track_id: str, body: MetadataBody):
    try:
        return await get_music_station_store().update_metadata(track_id, **body.model_dump())
    except KeyError:
        raise HTTPException(404, "歌曲不存在")


@router.post("/tracks/{track_id}/lyrics/refresh")
async def refresh_lyrics(track_id: str):
    try:
        return await get_music_station_store().refresh_lyrics(track_id)
    except KeyError:
        raise HTTPException(404, "歌曲不存在")
    except Exception as exc:
        raise HTTPException(502, f"歌词获取失败：{exc}")


@router.post("/tracks/{track_id}/cache")
async def cache_track(track_id: str):
    try:
        return await get_music_station_store().cache_netease_audio(track_id)
    except KeyError:
        raise HTTPException(404, "歌曲不存在")
    except Exception as exc:
        raise HTTPException(502, f"歌曲缓存失败：{exc}")


@router.post("/upload")
async def upload_track(file: UploadFile = File(...)):
    store = get_music_station_store()
    try:
        ext = validate_audio_upload(file.filename or "", file.content_type or "", 0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    destination = store.audio_dir / f"local_{uuid.uuid4().hex}{ext}"
    total = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise ValueError("文件太大，最大 300MB")
                handle.write(chunk)
        track_id = await store.add_local_track(
            destination, file.filename or destination.name, file.content_type or "audio/mpeg"
        )
        track = await store.get_track(track_id)
        return track
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise HTTPException(400, str(exc))
        raise


@router.get("/tracks/{track_id}/audio")
async def track_audio(track_id: str, range_header: str | None = Header(None, alias="Range")):
    store = get_music_station_store()
    track = await store.get_track(track_id)
    if not track:
        raise HTTPException(404, "歌曲不存在")
    local_path = Path(track.get("local_audio_path") or "")
    if str(local_path) and local_path.is_file():
        mime_type = track.get("mime_type") or mimetypes.guess_type(local_path.name)[0] or "audio/mpeg"
        return local_audio_response(local_path, mime_type, range_header)
    if track.get("source_type") != "netease" or not track.get("external_id"):
        raise HTTPException(404, "音频文件不存在")
    url = get_audio_url(int(track["external_id"]))
    if not url:
        raise HTTPException(404, "当前无法获取在线播放地址")
    client = httpx.AsyncClient(timeout=60, follow_redirects=True)
    request_headers = {
        "Referer": "https://music.163.com/",
        "User-Agent": "Mozilla/5.0",
    }
    if range_header:
        request_headers["Range"] = range_header
    upstream = await client.send(client.build_request("GET", url, headers=request_headers), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(upstream.status_code, "在线播放失败")

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    for key in ("content-range", "content-length"):
        if upstream.headers.get(key):
            response_headers[key] = upstream.headers[key]
    content_type = upstream.headers.get("content-type") or "audio/mpeg"
    return StreamingResponse(
        stream_body(),
        status_code=206 if upstream.status_code == 206 else 200,
        media_type=content_type,
        headers=response_headers,
    )
