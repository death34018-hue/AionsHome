"""Independent storage and media helpers for the 点歌台 feature."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite


log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 300 * 1024 * 1024
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}


def validate_audio_upload(filename: str, content_type: str, size: int) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise ValueError("仅支持 MP3、M4A、AAC、WAV、FLAC 和 OGG")
    base_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if base_type and not (base_type.startswith("audio/") or base_type == "application/octet-stream"):
        raise ValueError("文件不是受支持的音频类型")
    if int(size) > MAX_UPLOAD_BYTES:
        raise ValueError("文件太大，最大 300MB")
    return ext


class MusicStationStore:
    def __init__(self, db_path: str | Path, data_dir: str | Path):
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.audio_dir = self.data_dir / "audio"
        self._cache_lock = asyncio.Lock()

    async def init(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS music_station_tracks (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL UNIQUE,
                    external_id TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    artist TEXT DEFAULT '',
                    album TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    cover_url TEXT DEFAULT '',
                    local_audio_path TEXT DEFAULT '',
                    mime_type TEXT DEFAULT '',
                    lyrics_lrc TEXT DEFAULT '',
                    translated_lrc TEXT DEFAULT '',
                    romanized_lrc TEXT DEFAULT '',
                    trim_start_ms INTEGER DEFAULT 0,
                    trim_end_ms INTEGER DEFAULT 0,
                    cache_status TEXT DEFAULT 'pending',
                    cache_error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS music_station_requests (
                    id TEXT PRIMARY KEY,
                    track_id TEXT NOT NULL,
                    requester_identity TEXT NOT NULL,
                    requester_name_snapshot TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT DEFAULT '',
                    source_message_id TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    FOREIGN KEY (track_id) REFERENCES music_station_tracks(id) ON DELETE CASCADE,
                    UNIQUE(track_id, source_type, source_message_id)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_music_station_requests_track "
                "ON music_station_requests(track_id, requested_at DESC)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS music_station_playlists (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS music_station_playlist_tracks (
                    playlist_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    added_at REAL NOT NULL,
                    PRIMARY KEY (playlist_id, track_id),
                    FOREIGN KEY (playlist_id) REFERENCES music_station_playlists(id) ON DELETE CASCADE,
                    FOREIGN KEY (track_id) REFERENCES music_station_tracks(id) ON DELETE CASCADE
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS music_station_ignored_requests (
                    source_type TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    PRIMARY KEY (source_type, source_message_id)
                )
            """)
            await db.commit()

    @staticmethod
    def _song_source(song: dict[str, Any]) -> tuple[str, str, str]:
        source_type = str(song.get("source_type") or "netease").strip().lower()
        if source_type == "netease":
            external_id = str(int(song["id"]))
            return source_type, f"netease:{external_id}", external_id
        source_key = str(song.get("source_key") or "").strip()
        if not source_key:
            raise ValueError("歌曲缺少稳定来源标识")
        return source_type, source_key, str(song.get("external_id") or "")

    async def record_request(
        self,
        song: dict[str, Any],
        requester_identity: str,
        requester_name: str,
        source_type: str,
        source_id: str,
        source_message_id: str,
        requested_at: float | None = None,
        from_history: bool = False,
    ) -> str:
        track_source, source_key, external_id = self._song_source(song)
        title = str(song.get("name") or song.get("title") or "未知歌曲").strip() or "未知歌曲"
        now = float(requested_at if requested_at is not None else time.time())
        track_id = "mst_" + uuid.uuid4().hex
        request_id = "msr_" + uuid.uuid4().hex
        request_source = str(source_type or "unknown").strip() or "unknown"
        message_id = str(source_message_id or request_id).strip() or request_id
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            if from_history:
                ignored = await (await db.execute(
                    "SELECT 1 FROM music_station_ignored_requests "
                    "WHERE source_type=? AND source_message_id=?",
                    (request_source, message_id),
                )).fetchone()
                if ignored:
                    return ""
            await db.execute(
                """
                INSERT INTO music_station_tracks (
                    id, source_type, source_key, external_id, title, artist, album,
                    duration_ms, cover_url, local_audio_path, mime_type,
                    lyrics_lrc, translated_lrc, romanized_lrc, cache_status,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET
                    title=CASE WHEN excluded.title != '' THEN excluded.title ELSE title END,
                    artist=CASE WHEN excluded.artist != '' THEN excluded.artist ELSE artist END,
                    album=CASE WHEN excluded.album != '' THEN excluded.album ELSE album END,
                    duration_ms=CASE WHEN excluded.duration_ms > 0 THEN excluded.duration_ms ELSE duration_ms END,
                    cover_url=CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE cover_url END,
                    lyrics_lrc=CASE WHEN excluded.lyrics_lrc != '' THEN excluded.lyrics_lrc ELSE lyrics_lrc END,
                    translated_lrc=CASE WHEN excluded.translated_lrc != '' THEN excluded.translated_lrc ELSE translated_lrc END,
                    romanized_lrc=CASE WHEN excluded.romanized_lrc != '' THEN excluded.romanized_lrc ELSE romanized_lrc END,
                    updated_at=excluded.updated_at
                """,
                (
                    track_id,
                    track_source,
                    source_key,
                    external_id,
                    title,
                    str(song.get("artist") or "").strip(),
                    str(song.get("album") or "").strip(),
                    max(0, int(song.get("duration") or song.get("duration_ms") or 0)),
                    str(song.get("cover") or song.get("cover_url") or "").strip(),
                    str(song.get("local_audio_path") or "").strip(),
                    str(song.get("mime_type") or "").strip(),
                    str(song.get("lyrics_lrc") or song.get("lyrics") or ""),
                    str(song.get("translated_lrc") or ""),
                    str(song.get("romanized_lrc") or ""),
                    str(song.get("cache_status") or ("cached" if song.get("local_audio_path") else "pending")),
                    now,
                    now,
                ),
            )
            row = await (await db.execute(
                "SELECT id FROM music_station_tracks WHERE source_key=?", (source_key,)
            )).fetchone()
            resolved_track_id = row[0]
            await db.execute(
                """
                INSERT OR IGNORE INTO music_station_requests (
                    id, track_id, requester_identity, requester_name_snapshot,
                    source_type, source_id, source_message_id, requested_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    resolved_track_id,
                    str(requester_identity or "ai").strip() or "ai",
                    str(requester_name or "AI").strip() or "AI",
                    request_source,
                    str(source_id or "").strip(),
                    message_id,
                    now,
                ),
            )
            await db.commit()
        return resolved_track_id

    async def get_track(self, track_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM music_station_tracks WHERE id=?", (track_id,)
            )).fetchone()
        return dict(row) if row else None

    async def list_tracks(self, playlist_id: str | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            params: tuple[Any, ...] = ()
            membership_join = ""
            membership_where = ""
            if playlist_id:
                exists = await (await db.execute(
                    "SELECT 1 FROM music_station_playlists WHERE id=?", (playlist_id,)
                )).fetchone()
                if not exists:
                    raise KeyError(playlist_id)
                membership_join = (
                    "JOIN music_station_playlist_tracks pt ON pt.track_id=t.id "
                )
                membership_where = "WHERE pt.playlist_id=?"
                params = (playlist_id,)
            tracks = await (await db.execute(f"""
                SELECT t.*, COUNT(r.id) AS request_count, MAX(r.requested_at) AS last_requested_at
                FROM music_station_tracks t
                {membership_join}
                LEFT JOIN music_station_requests r ON r.track_id=t.id
                {membership_where}
                GROUP BY t.id
                ORDER BY COALESCE(MAX(r.requested_at), t.created_at) DESC
            """, params)).fetchall()
            result = []
            for row in tracks:
                item = dict(row)
                requests = await (await db.execute("""
                    SELECT id, requester_identity,
                           requester_name_snapshot,
                           source_type, source_id, source_message_id, requested_at
                    FROM music_station_requests
                    WHERE track_id=?
                    ORDER BY requested_at DESC, id DESC
                """, (item["id"],))).fetchall()
                names = self._configured_names()
                item["requests"] = []
                for req in requests:
                    request_item = dict(req)
                    identity = request_item["requester_identity"]
                    request_item["requester_name"] = names.get(
                        identity, request_item["requester_name_snapshot"]
                    )
                    item["requests"].append(request_item)
                result.append(item)
        return result

    @staticmethod
    def _playlist_name(name: str) -> str:
        value = str(name or "").strip()
        if not value or len(value) > 40:
            raise ValueError("歌单名称需要 1–40 个字符")
        return value

    async def list_playlists(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT p.*, COUNT(pt.track_id) AS track_count
                FROM music_station_playlists p
                LEFT JOIN music_station_playlist_tracks pt ON pt.playlist_id=p.id
                GROUP BY p.id
                ORDER BY p.sort_order, p.created_at, p.id
            """)).fetchall()
        return [dict(row) for row in rows]

    async def create_playlist(self, name: str) -> dict[str, Any]:
        value = self._playlist_name(name)
        playlist_id = "msp_" + uuid.uuid4().hex
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO music_station_playlists "
                    "(id,name,sort_order,created_at,updated_at) "
                    "VALUES (?,?,(SELECT COALESCE(MAX(sort_order),-1)+1 FROM music_station_playlists),?,?)",
                    (playlist_id, value, now, now),
                )
                await db.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("已经有同名歌单") from exc
        return next(item for item in await self.list_playlists() if item["id"] == playlist_id)

    async def rename_playlist(self, playlist_id: str, name: str) -> dict[str, Any]:
        value = self._playlist_name(name)
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    "UPDATE music_station_playlists SET name=?,updated_at=? WHERE id=?",
                    (value, time.time(), playlist_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(playlist_id)
                await db.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("已经有同名歌单") from exc
        return next(item for item in await self.list_playlists() if item["id"] == playlist_id)

    async def delete_playlist(self, playlist_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "DELETE FROM music_station_playlists WHERE id=?", (playlist_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(playlist_id)
            await db.commit()

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        ids = list(dict.fromkeys(str(item) for item in track_ids if str(item)))
        if not ids:
            raise ValueError("请选择歌曲")
        async with aiosqlite.connect(self.db_path) as db:
            playlist = await (await db.execute(
                "SELECT 1 FROM music_station_playlists WHERE id=?", (playlist_id,)
            )).fetchone()
            if not playlist:
                raise KeyError(playlist_id)
            placeholders = ",".join("?" for _ in ids)
            rows = await (await db.execute(
                f"SELECT id FROM music_station_tracks WHERE id IN ({placeholders})", ids
            )).fetchall()
            if len(rows) != len(ids):
                raise KeyError("track")
            before = db.total_changes
            now = time.time()
            await db.executemany(
                "INSERT OR IGNORE INTO music_station_playlist_tracks "
                "(playlist_id,track_id,sort_order,added_at) "
                "VALUES (?,?,(SELECT COALESCE(MAX(sort_order),-1)+1 "
                "FROM music_station_playlist_tracks WHERE playlist_id=?),?)",
                [(playlist_id, track_id, playlist_id, now) for track_id in ids],
            )
            await db.commit()
            return db.total_changes - before

    async def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        ids = list(dict.fromkeys(str(item) for item in track_ids if str(item)))
        if not ids:
            raise ValueError("请选择歌曲")
        async with aiosqlite.connect(self.db_path) as db:
            playlist = await (await db.execute(
                "SELECT 1 FROM music_station_playlists WHERE id=?", (playlist_id,)
            )).fetchone()
            if not playlist:
                raise KeyError(playlist_id)
            placeholders = ",".join("?" for _ in ids)
            cursor = await db.execute(
                f"DELETE FROM music_station_playlist_tracks "
                f"WHERE playlist_id=? AND track_id IN ({placeholders})",
                (playlist_id, *ids),
            )
            await db.commit()
            return max(0, cursor.rowcount)

    async def delete_tracks(self, track_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item) for item in track_ids if str(item)))
        if not ids:
            raise ValueError("请选择歌曲")
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            rows = await (await db.execute(
                f"SELECT id,local_audio_path FROM music_station_tracks "
                f"WHERE id IN ({placeholders})", ids,
            )).fetchall()
            if not rows:
                raise KeyError("track")
            await db.execute(
                f"INSERT OR IGNORE INTO music_station_ignored_requests "
                f"(source_type,source_message_id) "
                f"SELECT source_type,source_message_id FROM music_station_requests "
                f"WHERE track_id IN ({placeholders}) AND source_message_id!=''",
                ids,
            )
            cursor = await db.execute(
                f"DELETE FROM music_station_tracks WHERE id IN ({placeholders})", ids
            )
            await db.commit()
        warnings: list[str] = []
        owned_roots = [self.audio_dir.resolve()]
        try:
            from config import SONGS_DIR
            owned_roots.append(Path(SONGS_DIR).resolve())
        except Exception:
            pass
        for _, raw_path in rows:
            if not raw_path:
                continue
            try:
                path = Path(raw_path).resolve()
                if not any(path == root or root in path.parents for root in owned_roots):
                    continue
                path.unlink(missing_ok=True)
            except Exception as exc:
                warnings.append(str(exc))
        return {"deleted": max(0, cursor.rowcount), "warnings": warnings}

    async def refresh_lyrics(self, track_id: str, lyric_fetcher=None) -> dict[str, Any]:
        track = await self.get_track(track_id)
        if not track:
            raise KeyError(track_id)
        if track["source_type"] != "netease" or not track["external_id"]:
            return track
        if lyric_fetcher is None:
            from pyncm.apis.track import GetTrackLyrics
            payload = await asyncio.to_thread(GetTrackLyrics, str(track["external_id"]))
        else:
            payload = lyric_fetcher(str(track["external_id"]))
            if inspect.isawaitable(payload):
                payload = await payload
        payload = payload if isinstance(payload, dict) else {}
        original = str((payload.get("lrc") or {}).get("lyric") or "")
        translated = str((payload.get("tlyric") or {}).get("lyric") or "")
        romanized = str((payload.get("romalrc") or {}).get("lyric") or "")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE music_station_tracks
                   SET lyrics_lrc=?, translated_lrc=?, romanized_lrc=?, updated_at=?
                   WHERE id=?""",
                (original, translated, romanized, time.time(), track_id),
            )
            await db.commit()
        updated = await self.get_track(track_id)
        assert updated is not None
        return updated

    async def add_local_track(
        self,
        file_path: str | Path,
        original_name: str,
        mime_type: str,
    ) -> str:
        path = Path(file_path).resolve()
        track_id = "mst_" + uuid.uuid4().hex
        now = time.time()
        title = Path(original_name or path.name).stem or "本地歌曲"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO music_station_tracks (
                    id, source_type, source_key, title, local_audio_path, mime_type,
                    cache_status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (track_id, "local", f"local:{track_id}", title, str(path), mime_type,
                 "cached", now, now),
            )
            await db.commit()
        return track_id

    async def update_metadata(
        self,
        track_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        lyrics_lrc: str | None = None,
    ) -> dict[str, Any]:
        track = await self.get_track(track_id)
        if not track:
            raise KeyError(track_id)
        values = {
            "title": track["title"] if title is None else (title.strip() or "未知歌曲"),
            "artist": track["artist"] if artist is None else artist.strip(),
            "album": track["album"] if album is None else album.strip(),
            "lyrics_lrc": track["lyrics_lrc"] if lyrics_lrc is None else lyrics_lrc,
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE music_station_tracks
                   SET title=?, artist=?, album=?, lyrics_lrc=?, updated_at=? WHERE id=?""",
                (values["title"], values["artist"], values["album"],
                 values["lyrics_lrc"], time.time(), track_id),
            )
            await db.commit()
        updated = await self.get_track(track_id)
        assert updated is not None
        return updated

    async def set_cache_result(
        self,
        track_id: str,
        status: str,
        local_audio_path: str = "",
        mime_type: str = "",
        error: str = "",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE music_station_tracks
                   SET cache_status=?, local_audio_path=?,
                       mime_type=CASE WHEN ?!='' THEN ? ELSE mime_type END,
                       cache_error=?, updated_at=? WHERE id=?""",
                (status, local_audio_path, mime_type, mime_type, error[:500], time.time(), track_id),
            )
            await db.commit()

    async def cache_netease_audio(
        self,
        track_id: str,
        audio_url_fetcher=None,
        downloader=None,
    ) -> dict[str, Any]:
        track = await self.get_track(track_id)
        if not track:
            raise KeyError(track_id)
        if track["source_type"] != "netease" or not track["external_id"]:
            return track
        current = Path(track.get("local_audio_path") or "")
        if str(current) and current.is_file():
            return track
        async with self._cache_lock:
            track = await self.get_track(track_id)
            current = Path((track or {}).get("local_audio_path") or "")
            if track and str(current) and current.is_file():
                return track
            destination = (self.audio_dir / f"netease_{track['external_id']}.audio").resolve()
            destination.relative_to(self.audio_dir.resolve())
            try:
                if audio_url_fetcher is None:
                    from music import get_audio_url
                    url = await asyncio.to_thread(get_audio_url, int(track["external_id"]))
                else:
                    url = audio_url_fetcher(int(track["external_id"]))
                    if inspect.isawaitable(url):
                        url = await url
                if not url:
                    raise RuntimeError("当前无法获取播放地址")
                if downloader is None:
                    mime_type = await self._download_audio(str(url), destination)
                else:
                    mime_type = downloader(str(url), destination)
                    if inspect.isawaitable(mime_type):
                        mime_type = await mime_type
                if not destination.is_file() or destination.stat().st_size <= 0:
                    raise RuntimeError("下载结果为空")
                await self.set_cache_result(
                    track_id, "cached", str(destination), str(mime_type or "audio/mpeg")
                )
            except Exception as exc:
                destination.unlink(missing_ok=True)
                await self.set_cache_result(track_id, "failed", error=str(exc))
                raise
        updated = await self.get_track(track_id)
        assert updated is not None
        return updated

    @staticmethod
    async def _download_audio(url: str, destination: Path) -> str:
        import httpx

        temp_path = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                async with client.stream("GET", url, headers={
                    "Referer": "https://music.163.com/",
                    "User-Agent": "Mozilla/5.0",
                }) as response:
                    response.raise_for_status()
                    mime_type = response.headers.get("content-type", "audio/mpeg").split(";", 1)[0]
                    with temp_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > 500 * 1024 * 1024:
                                raise ValueError("歌曲缓存超过 500MB 限制")
                            handle.write(chunk)
            os.replace(temp_path, destination)
            return mime_type
        finally:
            temp_path.unlink(missing_ok=True)

    async def update_trim(self, track_id: str, start_ms: int, end_ms: int) -> dict[str, Any]:
        start_ms = int(start_ms)
        end_ms = int(end_ms)
        track = await self.get_track(track_id)
        if not track:
            raise KeyError(track_id)
        duration_ms = max(0, int(track.get("duration_ms") or 0))
        if start_ms < 0 or end_ms < 0 or (end_ms and start_ms >= end_ms):
            raise ValueError("裁剪区间无效")
        if duration_ms and (start_ms >= duration_ms or end_ms > duration_ms):
            raise ValueError("裁剪区间超出歌曲时长")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE music_station_tracks SET trim_start_ms=?, trim_end_ms=?, updated_at=? WHERE id=?",
                (start_ms, end_ms, time.time(), track_id),
            )
            await db.commit()
        updated = await self.get_track(track_id)
        assert updated is not None
        return updated

    @staticmethod
    def _decode_attachments(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            values = raw
        else:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, ValueError):
                return []
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def _attachment_song(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if item.get("type") == "music" and item.get("id"):
            return dict(item)
        if item.get("type") == "generated_song" and item.get("url"):
            url = str(item["url"])
            local_path = ""
            if url.startswith("/songs/"):
                try:
                    from config import SONGS_DIR
                    candidate = (SONGS_DIR / Path(url).name).resolve()
                    candidate.relative_to(SONGS_DIR.resolve())
                    if candidate.is_file():
                        local_path = str(candidate)
                except Exception:
                    local_path = ""
            return {
                "source_type": "generated",
                "source_key": f"generated:{url}",
                "title": item.get("title") or "AI 生成歌曲",
                "artist": item.get("artist") or "",
                "mime_type": item.get("mime_type") or "audio/mpeg",
                "local_audio_path": local_path,
                "cache_status": "cached" if local_path else "failed",
                "lyrics_lrc": item.get("lyrics") or "",
            }
        return None

    @staticmethod
    def _configured_names() -> dict[str, str]:
        names = {"aion": "AI", "connor": "第二AI", "user": "用户"}
        try:
            from chatroom import get_chatroom_names
            user_name, ai_name, connor_name = get_chatroom_names()
            names.update({"user": user_name, "aion": ai_name, "connor": connor_name})
        except Exception:
            try:
                from config import load_worldbook
                wb = load_worldbook()
                names["user"] = wb.get("user_name") or names["user"]
                names["aion"] = wb.get("ai_name") or names["aion"]
            except Exception:
                pass
        return names

    async def _request_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM music_station_requests")).fetchone()
        return int(row[0])

    async def backfill_history(self) -> int:
        """Import legacy music attachments. Safe to run repeatedly."""
        before = await self._request_count()
        names = self._configured_names()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            table_rows = await (await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).fetchall()
            tables = {row["name"] for row in table_rows}
            private_rows = []
            room_rows = []
            if "messages" in tables:
                private_rows = await (await db.execute(
                    "SELECT id, conv_id, role, attachments, created_at FROM messages "
                    "WHERE role='assistant' AND attachments IS NOT NULL AND attachments!=''"
                )).fetchall()
            if "chatroom_messages" in tables:
                room_rows = await (await db.execute(
                    "SELECT id, room_id, sender, attachments, created_at FROM chatroom_messages "
                    "WHERE attachments IS NOT NULL AND attachments!=''"
                )).fetchall()

        for row in private_rows:
            for item in self._decode_attachments(row["attachments"]):
                song = self._attachment_song(item)
                if not song:
                    continue
                if item.get("lyrics") and not song.get("lyrics_lrc"):
                    song["lyrics_lrc"] = item["lyrics"]
                await self.record_request(
                    song,
                    requester_identity="aion",
                    requester_name=names["aion"],
                    source_type="private",
                    source_id=row["conv_id"],
                    source_message_id=row["id"],
                    requested_at=row["created_at"],
                    from_history=True,
                )

        for row in room_rows:
            identity = str(row["sender"] or "ai")
            requester_name = names.get(identity, identity or "AI")
            for item in self._decode_attachments(row["attachments"]):
                song = self._attachment_song(item)
                if not song:
                    continue
                if item.get("lyrics") and not song.get("lyrics_lrc"):
                    song["lyrics_lrc"] = item["lyrics"]
                await self.record_request(
                    song,
                    requester_identity=identity,
                    requester_name=requester_name,
                    source_type="chatroom",
                    source_id=row["room_id"],
                    source_message_id=row["id"],
                    requested_at=row["created_at"],
                    from_history=True,
                )

        return (await self._request_count()) - before


_default_store: MusicStationStore | None = None


def get_music_station_store() -> MusicStationStore:
    global _default_store
    if _default_store is None:
        from config import DATA_DIR, DB_PATH
        _default_store = MusicStationStore(DB_PATH, DATA_DIR / "music_station")
    return _default_store


async def init_music_station() -> int:
    store = get_music_station_store()
    await store.init()
    return await store.backfill_history()


async def record_music_request(
    song: dict[str, Any],
    requester_identity: str,
    requester_name: str,
    source_type: str,
    source_id: str,
    source_message_id: str,
) -> str:
    store = get_music_station_store()
    track_id = await store.record_request(
        song,
        requester_identity=requester_identity,
        requester_name=requester_name,
        source_type=source_type,
        source_id=source_id,
        source_message_id=source_message_id,
    )
    asyncio.create_task(_refresh_new_netease_track(track_id))
    return track_id


async def _refresh_new_netease_track(track_id: str) -> None:
    store = get_music_station_store()
    try:
        await store.refresh_lyrics(track_id)
    except Exception as exc:
        log.info("点歌台歌词缓存失败 %s: %s", track_id, exc)
    try:
        await store.cache_netease_audio(track_id)
    except Exception as exc:
        log.info("点歌台音频缓存失败 %s: %s", track_id, exc)
