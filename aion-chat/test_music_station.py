import json
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import music_station
from music_station import MusicStationStore, validate_audio_upload
from routes.music_station import local_audio_response, router as music_station_router


class MusicStationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "chat.db"
        self.store = MusicStationStore(self.db_path, self.root / "music_station")
        await self.store.init()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_same_netease_track_keeps_each_companion_request(self):
        song = {
            "id": 42,
            "name": "同一首歌",
            "artist": "歌手",
            "album": "专辑",
            "duration": 180_000,
        }

        first_id = await self.store.record_request(
            song,
            requester_identity="aion",
            requester_name="伴侣甲",
            source_type="private",
            source_id="conv-1",
            source_message_id="msg-1",
            requested_at=100.0,
        )
        second_id = await self.store.record_request(
            song,
            requester_identity="connor",
            requester_name="伴侣乙",
            source_type="chatroom",
            source_id="room-1",
            source_message_id="msg-2",
            requested_at=200.0,
        )

        tracks = await self.store.list_tracks()
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["request_count"], 2)
        self.assertEqual(
            [(item["requester_identity"], item["requester_name_snapshot"], item["requested_at"])
             for item in tracks[0]["requests"]],
            [("connor", "伴侣乙", 200.0), ("aion", "伴侣甲", 100.0)],
        )
        self.assertTrue(all(item["requester_name"] for item in tracks[0]["requests"]))

    async def test_repeated_delivery_of_same_message_is_idempotent(self):
        song = {"id": 42, "name": "同一首歌", "artist": "歌手", "duration": 10_000}
        for _ in range(2):
            await self.store.record_request(
                song,
                requester_identity="aion",
                requester_name="伴侣甲",
                source_type="private",
                source_id="conv-1",
                source_message_id="msg-same",
                requested_at=100.0,
            )

        tracks = await self.store.list_tracks()
        self.assertEqual(tracks[0]["request_count"], 1)

    async def test_trim_must_stay_inside_track_duration(self):
        track_id = await self.store.record_request(
            {"id": 7, "name": "短歌", "artist": "歌手", "duration": 10_000},
            requester_identity="aion",
            requester_name="伴侣甲",
            source_type="private",
            source_id="conv-1",
            source_message_id="msg-7",
        )

        updated = await self.store.update_trim(track_id, 1_000, 8_000)
        self.assertEqual((updated["trim_start_ms"], updated["trim_end_ms"]), (1_000, 8_000))

        with self.assertRaises(ValueError):
            await self.store.update_trim(track_id, -1, 8_000)
        with self.assertRaises(ValueError):
            await self.store.update_trim(track_id, 8_000, 7_000)
        with self.assertRaises(ValueError):
            await self.store.update_trim(track_id, 1_000, 11_000)

    async def test_history_backfill_is_idempotent_and_preserves_requesters(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT,
                attachments TEXT, created_at REAL
            );
            CREATE TABLE chatroom_messages (
                id TEXT PRIMARY KEY, room_id TEXT, sender TEXT,
                attachments TEXT, created_at REAL
            );
        """)
        private_attachment = json.dumps([
            {"type": "music", "id": 42, "name": "同一首歌", "artist": "歌手"}
        ], ensure_ascii=False)
        room_attachment = json.dumps([
            {"type": "music", "id": 42, "name": "同一首歌", "artist": "歌手"}
        ], ensure_ascii=False)
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?)",
            ("private-msg", "conv-1", "assistant", private_attachment, 100.0),
        )
        conn.execute(
            "INSERT INTO chatroom_messages VALUES (?,?,?,?,?)",
            ("room-msg", "room-1", "connor", room_attachment, 200.0),
        )
        conn.commit()
        conn.close()

        self.assertEqual(await self.store.backfill_history(), 2)
        self.assertEqual(await self.store.backfill_history(), 0)

        tracks = await self.store.list_tracks()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["request_count"], 2)
        self.assertEqual(
            {item["requester_identity"] for item in tracks[0]["requests"]},
            {"aion", "connor"},
        )

    async def test_netease_lyrics_are_stored_with_translation(self):
        track_id = await self.store.record_request(
            {"id": 99, "name": "歌词歌", "artist": "歌手"},
            requester_identity="aion",
            requester_name="伴侣甲",
            source_type="private",
            source_id="conv-1",
            source_message_id="lyrics-msg",
        )
        payload = {
            "lrc": {"lyric": "[00:01.00]第一句"},
            "tlyric": {"lyric": "[00:01.00]First line"},
            "romalrc": {"lyric": ""},
        }

        await self.store.refresh_lyrics(track_id, lyric_fetcher=lambda _: payload)

        track = await self.store.get_track(track_id)
        self.assertEqual(track["lyrics_lrc"], "[00:01.00]第一句")
        self.assertEqual(track["translated_lrc"], "[00:01.00]First line")

    async def test_netease_audio_cache_writes_station_owned_file(self):
        track_id = await self.store.record_request(
            {"id": 100, "name": "缓存歌", "artist": "歌手"},
            requester_identity="aion",
            requester_name="伴侣甲",
            source_type="private",
            source_id="conv-1",
            source_message_id="cache-msg",
        )

        async def downloader(_url, destination):
            destination.write_bytes(b"fake-audio")
            return "audio/mpeg"

        updated = await self.store.cache_netease_audio(
            track_id,
            audio_url_fetcher=lambda _: "https://example.invalid/song.mp3",
            downloader=downloader,
        )

        cached_path = Path(updated["local_audio_path"])
        self.assertTrue(cached_path.is_file())
        self.assertEqual(cached_path.read_bytes(), b"fake-audio")
        self.assertEqual(updated["cache_status"], "cached")
        self.assertEqual(cached_path.parent, self.store.audio_dir.resolve())

    async def test_track_can_belong_to_multiple_playlists_without_duplication(self):
        audio_file = self.store.audio_dir / "first.mp3"
        audio_file.write_bytes(b"audio")
        track_id = await self.store.add_local_track(audio_file, "first.mp3", "audio/mpeg")
        work = await self.store.create_playlist("干活听的")
        sleep = await self.store.create_playlist("睡觉听的")

        await self.store.add_tracks_to_playlist(work["id"], [track_id, track_id])
        await self.store.add_tracks_to_playlist(sleep["id"], [track_id])

        self.assertEqual(len(await self.store.list_tracks(work["id"])), 1)
        self.assertEqual(len(await self.store.list_tracks(sleep["id"])), 1)
        playlists = await self.store.list_playlists()
        self.assertEqual({item["name"]: item["track_count"] for item in playlists}, {
            "干活听的": 1,
            "睡觉听的": 1,
        })
        await self.store.delete_playlist(work["id"])
        self.assertIsNotNone(await self.store.get_track(track_id))

    async def test_deleted_track_ignores_old_history_but_new_request_restores_it(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT,
                attachments TEXT, created_at REAL
            )
        """)
        attachment = json.dumps([
            {"type": "music", "id": 321, "name": "会再回来的歌", "artist": "歌手"}
        ], ensure_ascii=False)
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?)",
            ("old-message", "conv-1", "assistant", attachment, 10.0),
        )
        conn.commit()
        conn.close()

        self.assertEqual(await self.store.backfill_history(), 1)
        track_id = (await self.store.list_tracks())[0]["id"]
        result = await self.store.delete_tracks([track_id])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(await self.store.backfill_history(), 0)
        self.assertEqual(await self.store.list_tracks(), [])

        await self.store.record_request(
            {"id": 321, "name": "会再回来的歌", "artist": "歌手"},
            requester_identity="aion",
            requester_name="伴侣甲",
            source_type="private",
            source_id="conv-2",
            source_message_id="new-message",
            requested_at=20.0,
        )
        self.assertEqual(len(await self.store.list_tracks()), 1)

    async def test_delete_local_track_removes_owned_audio_file(self):
        audio_file = self.store.audio_dir / "delete-me.mp3"
        audio_file.write_bytes(b"audio")
        track_id = await self.store.add_local_track(audio_file, "delete-me.mp3", "audio/mpeg")

        result = await self.store.delete_tracks([track_id])

        self.assertEqual(result, {"deleted": 1, "warnings": []})
        self.assertFalse(audio_file.exists())

    def test_upload_validation_uses_extension_size_and_audio_type(self):
        self.assertEqual(validate_audio_upload("demo.MP3", "audio/mpeg", 1024), ".mp3")
        self.assertEqual(validate_audio_upload("demo.flac", "application/octet-stream", 1024), ".flac")
        with self.assertRaises(ValueError):
            validate_audio_upload("demo.exe", "application/octet-stream", 1024)
        with self.assertRaises(ValueError):
            validate_audio_upload("demo.mp3", "audio/mpeg", 300 * 1024 * 1024 + 1)

    def test_local_audio_range_returns_exact_requested_bytes(self):
        audio_path = self.root / "sample.mp3"
        audio_path.write_bytes(b"0123456789")

        response = local_audio_response(audio_path, "audio/mpeg", "bytes=2-5")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.body, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(response.headers["accept-ranges"], "bytes")


class MusicStationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MusicStationStore(self.root / "chat.db", self.root / "music_station")
        asyncio.run(self.store.init())
        audio_file = self.store.audio_dir / "api-song.mp3"
        audio_file.write_bytes(b"audio")
        self.track_id = asyncio.run(
            self.store.add_local_track(audio_file, "api-song.mp3", "audio/mpeg")
        )
        self.previous_store = music_station._default_store
        music_station._default_store = self.store
        app = FastAPI()
        app.include_router(music_station_router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        music_station._default_store = self.previous_store
        self.tmp.cleanup()

    def test_playlist_api_keeps_tracks_when_playlist_is_deleted(self):
        response = self.client.post(
            "/api/music-station/playlists", json={"name": "干活听的"}
        )
        self.assertEqual(response.status_code, 200)
        playlist = response.json()
        response = self.client.post(
            f"/api/music-station/playlists/{playlist['id']}/tracks",
            json={"track_ids": [self.track_id]},
        )
        self.assertEqual(response.status_code, 200)
        filtered = self.client.get(
            f"/api/music-station/tracks?playlist_id={playlist['id']}"
        ).json()["tracks"]
        self.assertEqual([item["id"] for item in filtered], [self.track_id])

        self.assertEqual(
            self.client.delete(f"/api/music-station/playlists/{playlist['id']}").status_code,
            200,
        )
        self.assertEqual(
            [item["id"] for item in self.client.get("/api/music-station/tracks").json()["tracks"]],
            [self.track_id],
        )
        deleted = self.client.request(
            "DELETE", "/api/music-station/tracks", json={"track_ids": [self.track_id]}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
