import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TheaterTTSCacheTests(unittest.TestCase):
    def test_maps_merged_and_segment_names_to_the_same_message_id(self):
        from theater_tts_cache import message_id_from_audio_path

        self.assertEqual(message_id_from_audio_path(Path("tm_123_ai.mp3")), "tm_123_ai")
        self.assertEqual(message_id_from_audio_path(Path("tm_123_ai_s12.mp3")), "tm_123_ai")
        self.assertIsNone(message_id_from_audio_path(Path("tm_123_ai.tmp")))

    def test_deletes_only_exact_message_merged_and_segment_files(self):
        from theater_tts_cache import delete_message_audio_files

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            targets = [
                cache_dir / "tm_delete.mp3",
                cache_dir / "tm_delete_s0.mp3",
                cache_dir / "tm_delete_s10.mp3",
            ]
            keep = [
                cache_dir / "tm_delete_other.mp3",
                cache_dir / "tm_delete_something.mp3",
                cache_dir / "tm_delete_s1_backup.mp3",
                cache_dir / "tm_keep_s0.mp3",
                cache_dir / "notes.txt",
            ]
            for path in [*targets, *keep]:
                path.write_bytes(b"audio")

            deleted = delete_message_audio_files(["tm_delete"], cache_dir)

            self.assertEqual([path.name for path in deleted], [path.name for path in targets])
            self.assertTrue(all(not path.exists() for path in targets))
            self.assertTrue(all(path.exists() for path in keep))

    def test_orphan_discovery_preserves_valid_and_recent_audio(self):
        from theater_tts_cache import find_orphan_audio_files

        now = 10_000.0
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            valid = cache_dir / "tm_keep.mp3"
            old_orphan = cache_dir / "tm_old_s2.mp3"
            recent_orphan = cache_dir / "tm_recent.mp3"
            ignored = cache_dir / "tm_old.tmp"
            for path in (valid, old_orphan, recent_orphan, ignored):
                path.write_bytes(b"audio")
            os.utime(valid, (now - 7200, now - 7200))
            os.utime(old_orphan, (now - 7200, now - 7200))
            os.utime(recent_orphan, (now - 60, now - 60))
            os.utime(ignored, (now - 7200, now - 7200))

            orphans = find_orphan_audio_files(
                {"tm_keep"},
                cache_dir,
                min_age_seconds=3600,
                now=now,
            )

            self.assertEqual([path.name for path in orphans], ["tm_old_s2.mp3"])

    def test_one_locked_file_does_not_block_other_cache_deletions(self):
        from theater_tts_cache import delete_message_audio_files

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            locked = cache_dir / "tm_locked_s0.mp3"
            removable = cache_dir / "tm_locked_s1.mp3"
            merged = cache_dir / "tm_locked.mp3"
            for path in (locked, removable, merged):
                path.write_bytes(b"audio")

            original_unlink = Path.unlink
            locked_attempts = 0

            def flaky_unlink(path, *args, **kwargs):
                nonlocal locked_attempts
                if path == locked:
                    locked_attempts += 1
                    raise PermissionError("file is in use")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=flaky_unlink):
                deleted = delete_message_audio_files(["tm_locked"], cache_dir)

            self.assertTrue(locked.exists())
            self.assertFalse(removable.exists())
            self.assertFalse(merged.exists())
            self.assertEqual(locked_attempts, 3)
            self.assertEqual(
                [path.name for path in deleted],
                ["tm_locked.mp3", "tm_locked_s1.mp3"],
            )

    def test_segment_manifest_lists_every_available_numeric_segment_in_order(self):
        from theater_tts_cache import list_message_audio_segments

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            for name in (
                "tm_manifest_s12.mp3",
                "tm_manifest_s0.mp3",
                "tm_manifest_s2.mp3",
                "tm_manifest_something.mp3",
                "tm_other_s1.mp3",
            ):
                (cache_dir / name).write_bytes(b"audio")

            segments = list_message_audio_segments("tm_manifest", cache_dir)

            self.assertEqual(
                [(seq, path.name) for seq, path in segments],
                [
                    (0, "tm_manifest_s0.mp3"),
                    (2, "tm_manifest_s2.mp3"),
                    (12, "tm_manifest_s12.mp3"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
