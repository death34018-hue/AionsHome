import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _create_theater_schema(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE theater_conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            persona_id TEXT,
            model TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE theater_messages (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            attachments TEXT DEFAULT '[]',
            reasoning_content TEXT DEFAULT '',
            FOREIGN KEY (conv_id) REFERENCES theater_conversations(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_conversation(db_path: Path, conv_id: str, message_ids: list[str]):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO theater_conversations VALUES (?,?,?,?,?,?)",
        (conv_id, "test", "", "test-model", 1.0, 1.0),
    )
    for index, message_id in enumerate(message_ids):
        conn.execute(
            "INSERT INTO theater_messages "
            "(id, conv_id, role, content, created_at, attachments) "
            "VALUES (?,?,?,?,?,?)",
            (message_id, conv_id, "assistant", f"story-{index}", index + 1.0, "[]"),
        )
    conn.commit()
    conn.close()


class TheaterAudioDeletionTests(unittest.TestCase):
    def _with_route_paths(self, db_path: Path, cache_dir: Path):
        import database
        from routes import theater as route

        old_db_path = database.DB_PATH
        old_cache_dir = route.THEATER_TTS_CACHE_DIR
        database.DB_PATH = db_path
        route.THEATER_TTS_CACHE_DIR = cache_dir
        return database, route, old_db_path, old_cache_dir

    def test_delete_message_removes_merged_and_segment_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "cache"
            cache_dir.mkdir()
            _create_theater_schema(db_path)
            _insert_conversation(db_path, "tc_one", ["tm_delete", "tm_keep"])
            for name in ("tm_delete.mp3", "tm_delete_s0.mp3", "tm_delete_s1.mp3", "tm_keep.mp3"):
                (cache_dir / name).write_bytes(b"audio")

            database, route, old_db_path, old_cache_dir = self._with_route_paths(db_path, cache_dir)
            try:
                asyncio.run(route.delete_message("tm_delete"))
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

            conn = sqlite3.connect(db_path)
            remaining = [row[0] for row in conn.execute("SELECT id FROM theater_messages ORDER BY id")]
            conn.close()
            self.assertEqual(remaining, ["tm_keep"])
            self.assertFalse((cache_dir / "tm_delete.mp3").exists())
            self.assertFalse((cache_dir / "tm_delete_s0.mp3").exists())
            self.assertFalse((cache_dir / "tm_delete_s1.mp3").exists())
            self.assertTrue((cache_dir / "tm_keep.mp3").exists())

    def test_delete_conversation_removes_audio_for_all_cascaded_messages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "cache"
            cache_dir.mkdir()
            _create_theater_schema(db_path)
            _insert_conversation(db_path, "tc_delete", ["tm_a", "tm_b"])
            _insert_conversation(db_path, "tc_keep", ["tm_keep"])
            for name in ("tm_a.mp3", "tm_a_s0.mp3", "tm_b.mp3", "tm_keep.mp3"):
                (cache_dir / name).write_bytes(b"audio")

            database, route, old_db_path, old_cache_dir = self._with_route_paths(db_path, cache_dir)
            try:
                asyncio.run(route.delete_conversation("tc_delete"))
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

            conn = sqlite3.connect(db_path)
            conversations = [row[0] for row in conn.execute("SELECT id FROM theater_conversations ORDER BY id")]
            messages = [row[0] for row in conn.execute("SELECT id FROM theater_messages ORDER BY id")]
            conn.close()
            self.assertEqual(conversations, ["tc_keep"])
            self.assertEqual(messages, ["tm_keep"])
            self.assertFalse((cache_dir / "tm_a.mp3").exists())
            self.assertFalse((cache_dir / "tm_a_s0.mp3").exists())
            self.assertFalse((cache_dir / "tm_b.mp3").exists())
            self.assertTrue((cache_dir / "tm_keep.mp3").exists())

    def test_segment_manifest_endpoint_returns_every_numeric_segment_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            for name in (
                "tm_manifest_s14.mp3",
                "tm_manifest_s2.mp3",
                "tm_manifest_s0.mp3",
                "tm_manifest_s2_backup.mp3",
                "tm_manifest_notes.mp3",
            ):
                (cache_dir / name).write_bytes(b"audio")

            from routes import theater as route

            old_cache_dir = route.THEATER_TTS_CACHE_DIR
            route.THEATER_TTS_CACHE_DIR = cache_dir
            try:
                result = asyncio.run(route.list_tts_segments("tm_manifest"))
            finally:
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

            self.assertEqual(
                result,
                {
                    "segments": [
                        {"seq": 0, "url": "/api/theater/tts/audio/tm_manifest_s0"},
                        {"seq": 2, "url": "/api/theater/tts/audio/tm_manifest_s2"},
                        {"seq": 14, "url": "/api/theater/tts/audio/tm_manifest_s14"},
                    ]
                },
            )

    def test_flush_waits_for_merged_audio_when_requested(self):
        from tts import TTSStreamer

        class EventSink:
            def __init__(self):
                self.events = []

            async def send_tts_event(self, payload):
                self.events.append(payload["type"])

        async def exercise(cache_dir: Path):
            sink = EventSink()
            streamer = TTSStreamer(
                "tm_merge_wait",
                "test-voice",
                sink,
                cache_dir=cache_dir,
                merge_segments=True,
                cache_max_bytes=None,
            )
            segment = cache_dir / "tm_merge_wait_s0.mp3"
            segment.write_bytes(b"segment-audio")
            streamer._seq = 1
            streamer._segment_paths[0] = segment

            await streamer.flush(wait_for_merge=True)

            self.assertTrue((cache_dir / "tm_merge_wait.mp3").exists())
            self.assertEqual(sink.events, ["tts_done", "tts_merged"])

        with tempfile.TemporaryDirectory() as td:
            asyncio.run(exercise(Path(td)))

    def test_streamer_includes_conversation_context_in_every_tts_event(self):
        import tts

        class EventSink:
            def __init__(self):
                self.events = []

            async def send_tts_event(self, payload):
                self.events.append(payload)

        async def exercise(cache_dir: Path):
            sink = EventSink()
            streamer = tts.TTSStreamer(
                "tm_context",
                "test-voice",
                sink,
                cache_dir=cache_dir,
                merge_segments=True,
                cache_max_bytes=None,
                event_data={"conv_id": "tc_context"},
            )
            with patch.object(tts, "_request_tts_audio", new=lambda *args, **kwargs: asyncio.sleep(0, result=b"audio")):
                streamer._dispatch("story")
                await streamer.flush(wait_for_merge=True)

            self.assertEqual(
                [event["type"] for event in sink.events],
                ["tts_chunk", "tts_done", "tts_merged"],
            )
            self.assertTrue(all(
                event["data"]["conv_id"] == "tc_context"
                for event in sink.events
            ))

        with tempfile.TemporaryDirectory() as td:
            asyncio.run(exercise(Path(td)))

    def test_late_tts_files_are_removed_when_message_was_deleted_during_flush(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "cache"
            cache_dir.mkdir()
            _create_theater_schema(db_path)

            database, route, old_db_path, old_cache_dir = self._with_route_paths(db_path, cache_dir)

            class LateWritingStreamer:
                async def flush(self, *, wait_for_merge=False):
                    self.wait_for_merge = wait_for_merge
                    (cache_dir / "tm_deleted.mp3").write_bytes(b"late-merged")
                    (cache_dir / "tm_deleted_s0.mp3").write_bytes(b"late-segment")

            streamer = LateWritingStreamer()
            try:
                asyncio.run(route._flush_and_cleanup_theater_tts(streamer, "tm_deleted"))
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

            self.assertTrue(streamer.wait_for_merge)
            self.assertFalse((cache_dir / "tm_deleted.mp3").exists())
            self.assertFalse((cache_dir / "tm_deleted_s0.mp3").exists())

    def test_late_assistant_reply_is_not_persisted_after_conversation_delete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "chat.db"
            cache_dir = root / "cache"
            cache_dir.mkdir()
            _create_theater_schema(db_path)

            database, route, old_db_path, old_cache_dir = self._with_route_paths(db_path, cache_dir)
            try:
                persisted = asyncio.run(route._persist_theater_assistant_message(
                    "tc_deleted",
                    "tm_late_ai",
                    "late story",
                    10.0,
                ))
            finally:
                database.DB_PATH = old_db_path
                route.THEATER_TTS_CACHE_DIR = old_cache_dir

            self.assertFalse(persisted)
            conn = sqlite3.connect(db_path)
            rows = list(conn.execute("SELECT id FROM theater_messages"))
            conn.close()
            self.assertEqual(rows, [])

    def test_cancelled_streamer_never_writes_or_notifies_late_audio(self):
        import tts

        class EventSink:
            def __init__(self):
                self.events = []

            async def send_tts_event(self, payload):
                self.events.append(payload["type"])

        async def exercise(cache_dir: Path):
            request_started = asyncio.Event()
            release_request = asyncio.Event()

            async def delayed_audio(*args, **kwargs):
                request_started.set()
                await release_request.wait()
                return b"late-audio"

            sink = EventSink()
            streamer = tts.TTSStreamer(
                "tm_cancelled",
                "test-voice",
                sink,
                cache_dir=cache_dir,
                merge_segments=True,
                cache_max_bytes=None,
            )
            with patch.object(tts, "_request_tts_audio", new=delayed_audio):
                streamer._dispatch("late segment")
                await request_started.wait()
                streamer.cancel()
                release_request.set()
                await streamer.flush(wait_for_merge=True)

            self.assertEqual(sink.events, [])
            self.assertEqual(list(cache_dir.glob("tm_cancelled*")), [])

        with tempfile.TemporaryDirectory() as td:
            asyncio.run(exercise(Path(td)))

    def test_conversation_delete_cancels_unpersisted_tts_streamers(self):
        from routes import theater as route

        class FakeStreamer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        first = FakeStreamer()
        second = FakeStreamer()
        route._active_theater_tts.clear()
        try:
            route._register_theater_tts("tc_delete", "tm_pending", first)
            route._register_theater_tts("tc_keep", "tm_keep", second)
            route._cancel_theater_tts_for_conversation("tc_delete")

            self.assertTrue(first.cancelled)
            self.assertFalse(second.cancelled)
            self.assertNotIn("tm_pending", route._active_theater_tts)
            self.assertIn("tm_keep", route._active_theater_tts)
        finally:
            route._active_theater_tts.clear()

    def test_streamer_registered_after_conversation_delete_is_cancelled_immediately(self):
        from routes import theater as route

        class FakeStreamer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        streamer = FakeStreamer()
        route._active_theater_tts.clear()
        route._deleted_theater_conversations.clear()
        try:
            route._deleted_theater_conversations.add("tc_deleted")
            registered = route._register_theater_tts("tc_deleted", "tm_late", streamer)

            self.assertFalse(registered)
            self.assertTrue(streamer.cancelled)
            self.assertNotIn("tm_late", route._active_theater_tts)
        finally:
            route._active_theater_tts.clear()
            route._deleted_theater_conversations.clear()

    def test_conversation_tombstone_rolls_back_when_database_delete_fails(self):
        from contextlib import asynccontextmanager
        from routes import theater as route

        @asynccontextmanager
        async def failing_db():
            raise sqlite3.OperationalError("database is busy")
            yield None

        route._deleted_theater_conversations.clear()
        try:
            with patch.object(route, "get_db", new=failing_db):
                with self.assertRaises(sqlite3.OperationalError):
                    asyncio.run(route.delete_conversation("tc_still_exists"))

            self.assertNotIn("tc_still_exists", route._deleted_theater_conversations)
        finally:
            route._deleted_theater_conversations.clear()


if __name__ == "__main__":
    unittest.main()
