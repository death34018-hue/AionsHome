import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from album import AlbumStore


def sample_image():
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "teal").save(output, "PNG")
    return output.getvalue()


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AlbumStore(Path(self.temp.name))

    def test_generated_photo_preserves_prompt_original_and_reference(self):
        original = sample_image()
        photo = self.store.save_photo(original, source="generated", prompt="exact prompt\n第二行",
                                      model="image-model", actor="aion", reference_bytes=original)
        self.assertEqual(photo["prompt"], "exact prompt\n第二行")
        self.assertEqual(photo["model"], "image-model")
        self.assertEqual((self.store.images_dir / photo["filename"]).read_bytes(), original)
        self.assertTrue((self.store.references_dir / photo["reference_filename"]).is_file())
        self.assertEqual((photo["width"], photo["height"]), (12, 8))
        self.assertTrue(photo["url"].startswith("/uploads/album/"))

    def test_remove_only_hides_record_and_keeps_files_after_reopening(self):
        photo = self.store.save_photo(sample_image(), source="upload", original_name="trip.png")
        self.store.remove_photo(photo["id"])
        reopened = AlbumStore(Path(self.temp.name))
        self.assertEqual(reopened.list_photos()["total"], 0)
        self.assertTrue((reopened.images_dir / photo["filename"]).is_file())
        self.assertTrue((reopened.thumbnails_dir / (photo["id"] + ".webp")).is_file())

    def test_date_edit_preserves_upload_time_and_invalid_image_is_not_saved(self):
        photo = self.store.save_photo(sample_image(), source="upload", taken_on="2020-01-02")
        updated = self.store.update_photo(photo["id"], taken_on="2021-03-04", title="旅行")
        self.assertEqual(updated["taken_on"], "2021-03-04")
        self.assertEqual(updated["created_at"], photo["created_at"])
        self.assertEqual(updated["prompt"], "")
        with self.assertRaises(ValueError):
            self.store.save_photo(b"<html>not a photo</html>", source="upload")
        self.assertEqual(self.store.list_photos()["total"], 1)

    def test_album_defaults_and_reclassification_do_not_move_or_change_files(self):
        original = sample_image()
        upload = self.store.save_photo(original, source="upload")
        generated = self.store.save_photo(original, source="generated", actor="connor", prompt="keep this")
        self.assertEqual(upload.get("album_id"), "family")
        self.assertEqual(generated.get("album_id"), "connor")
        updated = self.store.update_photo(generated["id"], album_id="family")
        self.assertEqual(updated["url"], generated["url"])
        self.assertEqual(updated["prompt"], "keep this")
        self.assertEqual((self.store.images_dir / generated["filename"]).read_bytes(), original)
        self.assertEqual(self.store.list_photos(album_id="family")["total"], 2)
        with self.assertRaises(ValueError):
            self.store.update_photo(upload["id"], album_id="unknown")

    def test_batch_move_changes_only_selected_categories_and_preserves_view_history(self):
        original = sample_image()
        photos = [self.store.save_photo(original, source="generated", album_id="family",
                                       prompt="keep prompt", taken_on="2020-01-02") for _ in range(3)]
        self.store.mark_viewed("aion", [photos[0]["id"]])
        selected = [p["id"] for p in photos[:2]]
        self.assertTrue(callable(getattr(self.store, "move_photos", None)), "batch move is missing")
        self.assertEqual(self.store.move_photos(selected + selected[:1], "connor"), 2)
        for photo in photos[:2]:
            self.assertEqual(self.store.get_photo(photo["id"]), {**photo, "album_id": "connor"})
            self.assertEqual((self.store.images_dir / photo["filename"]).read_bytes(), original)
        self.assertEqual(self.store.get_photo(photos[2]["id"])["album_id"], "family")
        self.store.move_photos(selected, "family")
        self.assertNotIn(photos[0]["id"], {p["id"] for p in self.store.random_unseen_photos("aion")})

    def test_batch_move_skips_removed_records_and_rejects_unknown_album(self):
        photo = self.store.save_photo(sample_image(), source="upload")
        removed = self.store.save_photo(sample_image(), source="upload")
        self.store.remove_photo(removed["id"])
        self.assertTrue(callable(getattr(self.store, "move_photos", None)), "batch move is missing")
        with self.assertRaises(ValueError):
            self.store.move_photos([photo["id"]], "unknown")
        self.assertEqual(self.store.get_photo(photo["id"])["album_id"], "family")
        self.assertEqual(self.store.move_photos([photo["id"], removed["id"], "missing"], "aion"), 1)
        self.assertIsNone(self.store.get_photo(removed["id"]))

    def test_view_history_reports_each_actor_without_marking_new_views(self):
        photo = self.store.save_photo(sample_image(), source="upload")
        self.assertTrue(callable(getattr(self.store, "get_photo_views", None)))
        self.assertEqual(self.store.get_photo_views(photo["id"]), [])
        self.store.mark_viewed("connor", [photo["id"]])
        views = self.store.get_photo_views(photo["id"])
        self.assertEqual([v["actor"] for v in views], ["connor"])
        self.assertGreater(views[0]["viewed_at"], 0)
        self.assertTrue(self.store.has_unseen_photos("aion"))
        self.store.mark_viewed("aion", [photo["id"]])
        self.assertEqual({v["actor"] for v in self.store.get_photo_views(photo["id"])}, {"aion", "connor"})

    def test_unseen_selection_is_family_only_and_independent_for_each_actor(self):
        self.assertTrue(hasattr(self.store, "random_unseen_photos"))
        family = [self.store.save_photo(sample_image(), source="upload") for _ in range(3)]
        self.store.save_photo(sample_image(), source="generated", actor="aion")
        removed = self.store.save_photo(sample_image(), source="upload")
        self.store.remove_photo(removed["id"])
        first = self.store.random_unseen_photos("aion")
        self.assertEqual(len(first), 2)
        self.assertTrue({p["id"] for p in first} <= {p["id"] for p in family})
        self.store.mark_viewed("aion", [p["id"] for p in first])
        reopened = AlbumStore(Path(self.temp.name))
        remaining = reopened.random_unseen_photos("aion")
        self.assertEqual(len(remaining), 1)
        self.assertNotIn(remaining[0]["id"], {p["id"] for p in first})
        self.assertEqual(len(reopened.random_unseen_photos("connor")), 2)
        reopened.mark_viewed("aion", [remaining[0]["id"]])
        self.assertEqual(reopened.random_unseen_photos("aion"), [])

    def test_legacy_photos_are_classified_once_without_resetting_user_choices(self):
        photo = self.store.save_photo(sample_image(), source="generated", actor="connor")
        with self.store._connect() as db:
            if "album_id" in {r[1] for r in db.execute("PRAGMA table_info(photos)")}:
                db.execute("DROP INDEX IF EXISTS photos_album")
                db.execute("ALTER TABLE photos DROP COLUMN album_id")
        migrated = AlbumStore(Path(self.temp.name))
        self.assertEqual(migrated.get_photo(photo["id"]).get("album_id"), "connor")
        migrated.update_photo(photo["id"], album_id="family")
        self.assertEqual(AlbumStore(Path(self.temp.name)).get_photo(photo["id"])["album_id"], "family")


if __name__ == "__main__":
    unittest.main()
