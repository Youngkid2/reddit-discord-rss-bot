"""Tests for bot settings persistence and media filtering."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "bot.db")
        os.environ["SQLITE_PATH"] = self.db_path
        db._backend_announced = False
        db.init_db()

    def tearDown(self):
        os.environ.pop("SQLITE_PATH", None)
        self.tmpdir.cleanup()

    def test_defaults_when_nothing_saved(self):
        self.assertEqual(db.get_check_interval_minutes(default=5), 5)
        self.assertEqual(db.get_check_interval_minutes(default=10), 10)
        self.assertEqual(db.get_media_type(), "both")

    def test_interval_and_media_persist_across_reinit(self):
        self.assertTrue(db.set_check_interval_minutes(2))
        self.assertTrue(db.set_media_type("videos"))
        self.assertEqual(db.get_check_interval_minutes(default=5), 2)
        self.assertEqual(db.get_media_type(), "videos")

        # Simulate a restart: same SQLite file, fresh init.
        db._backend_announced = False
        db.init_db()
        self.assertEqual(db.get_check_interval_minutes(default=5), 2)
        self.assertEqual(db.get_media_type(), "videos")

        self.assertTrue(db.set_check_interval_minutes(1))
        self.assertTrue(db.set_media_type("images"))
        db._backend_announced = False
        db.init_db()
        self.assertEqual(db.get_check_interval_minutes(default=5), 1)
        self.assertEqual(db.get_media_type(), "images")

        self.assertTrue(db.set_media_type("both"))
        self.assertTrue(db.set_check_interval_minutes(5))
        self.assertEqual(db.get_media_type(), "both")
        self.assertEqual(db.get_check_interval_minutes(), 5)

    def test_invalid_settings_are_rejected_or_fall_back(self):
        self.assertTrue(db.set_check_interval_minutes(2))
        self.assertTrue(db.set_media_type("videos"))

        self.assertFalse(db.set_check_interval_minutes(7))
        self.assertFalse(db.set_check_interval_minutes(0))
        self.assertFalse(db.set_check_interval_minutes("abc"))
        self.assertEqual(db.get_check_interval_minutes(default=5), 2)

        self.assertFalse(db.set_media_type("audio"))
        self.assertFalse(db.set_media_type(""))
        self.assertFalse(db.set_media_type("BOTH please"))
        self.assertEqual(db.get_media_type(), "videos")

        # Corrupt values already in the table must not crash; use defaults.
        self.assertTrue(db.set_bot_setting("check_interval_minutes", "99"))
        self.assertTrue(db.set_bot_setting("media_type", "nope"))
        self.assertEqual(db.get_check_interval_minutes(default=5), 5)
        self.assertEqual(db.get_media_type(), "both")

    def test_normalize_interval_keeps_env_default_if_not_1_2_or_5(self):
        self.assertEqual(db.normalize_interval(None, default=10), 10)
        self.assertEqual(db.normalize_interval("2", default=10), 2)
        self.assertEqual(db.normalize_interval("nope", default="x"), 5)
        self.assertEqual(db.parse_interval(1), 1)
        self.assertEqual(db.parse_interval(3), None)

    def test_setting_read_failure_falls_back(self):
        os.environ["SQLITE_PATH"] = str(Path(self.tmpdir.name) / "missing" / "nope.db")
        # Parent dir may be created by get_connection; point at a non-db instead.
        # Use a directory path so SQLite open fails.
        os.environ["SQLITE_PATH"] = self.tmpdir.name
        value = db.get_bot_setting("check_interval_minutes", default="fallback")
        self.assertEqual(value, "fallback")
        self.assertEqual(db.get_check_interval_minutes(default=5), 5)
        self.assertEqual(db.get_media_type(), "both")
        self.assertFalse(db.set_bot_setting("media_type", "both"))


class MediaFilterTests(unittest.TestCase):
    def setUp(self):
        self.image_post = {
            "image_urls": ["https://i.redd.it/a.jpg"],
            "video_urls": [],
        }
        self.video_with_thumb = {
            "image_urls": ["https://i.redd.it/thumb.jpg"],
            "video_urls": ["https://v.redd.it/a.mp4"],
        }
        self.video_only = {
            "image_urls": [],
            "video_urls": ["https://v.redd.it/a.mp4"],
        }
        self.empty_post = {"image_urls": [], "video_urls": []}
        self.missing_keys = {}

    def test_images_only(self):
        self.assertTrue(db.post_matches_media_filter(self.image_post, "images"))
        self.assertFalse(db.post_matches_media_filter(self.video_with_thumb, "images"))
        self.assertFalse(db.post_matches_media_filter(self.video_only, "images"))
        self.assertFalse(db.post_matches_media_filter(self.empty_post, "images"))
        self.assertFalse(db.post_matches_media_filter(self.missing_keys, "images"))

    def test_videos_only(self):
        self.assertFalse(db.post_matches_media_filter(self.image_post, "videos"))
        self.assertTrue(db.post_matches_media_filter(self.video_with_thumb, "videos"))
        self.assertTrue(db.post_matches_media_filter(self.video_only, "videos"))
        self.assertFalse(db.post_matches_media_filter(self.empty_post, "videos"))

    def test_both_and_invalid_fallback(self):
        self.assertTrue(db.post_matches_media_filter(self.image_post, "both"))
        self.assertTrue(db.post_matches_media_filter(self.video_with_thumb, "both"))
        self.assertTrue(db.post_matches_media_filter(self.video_only, "both"))
        self.assertFalse(db.post_matches_media_filter(self.empty_post, "both"))

        # Invalid or missing filter falls back to "both".
        self.assertTrue(db.post_matches_media_filter(self.image_post, "nope"))
        self.assertTrue(db.post_matches_media_filter(self.image_post, None))
        self.assertFalse(db.post_matches_media_filter(self.empty_post, "nope"))
        self.assertEqual(db.normalize_media_type("VIDEOS"), "videos")
        self.assertEqual(db.normalize_media_type("junk"), "both")


if __name__ == "__main__":
    unittest.main()
