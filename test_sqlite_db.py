"""Offline SQLite tests for db.py and bot startup without DATABASE_URL."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# The bot should work with no Postgres URL at all.
os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402


REPO_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


class SqliteDbTests(unittest.TestCase):
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

    def test_init_creates_sqlite_file(self):
        self.assertTrue(Path(self.db_path).is_file())
        self.assertFalse(db._use_postgres())

    def test_add_and_get_configs(self):
        db.add_subreddit_config("aww", "111")
        db.upsert_subreddit_config(
            subreddit_name="pics",
            discord_channel_id="222",
            feed_modes="hot,new",
            post_limit=10,
            discord_guild_id="guild-1",
        )

        rows = db.get_active_subreddit_configs()
        self.assertEqual(len(rows), 2)

        aww = next(row for row in rows if row[0] == "aww")
        self.assertEqual(aww[1], "111")
        self.assertEqual(aww[2], "old,hot,new")
        self.assertEqual(aww[3], 25)
        self.assertIsNone(aww[4])
        self.assertIsInstance(aww[5], bool)
        self.assertFalse(aww[5])

        pics = next(row for row in rows if row[0] == "pics")
        self.assertEqual(pics[1], "222")
        self.assertEqual(pics[2], "hot,new")
        self.assertEqual(pics[3], 10)
        self.assertEqual(pics[4], "guild-1")
        self.assertFalse(pics[5])

        listed = db.list_subreddit_configs("guild-1")
        self.assertEqual(len(listed), 1)
        row = listed[0]
        self.assertEqual(len(row), 8)
        self.assertEqual(row[1], "pics")
        self.assertEqual(row[2], "222")
        self.assertIsInstance(row[3], bool)
        self.assertTrue(row[3])
        self.assertEqual(row[4], "hot,new")
        self.assertEqual(row[5], 10)
        self.assertIsInstance(row[6], bool)
        self.assertFalse(row[6])
        self.assertIsNotNone(row[7])

    def test_has_been_posted_and_mark_as_posted(self):
        self.assertFalse(db.has_been_posted("p1"))
        self.assertFalse(db.has_been_processed("p1", "111"))

        db.mark_as_posted(
            post_id="p1",
            title="First",
            reddit_url="https://reddit.com/r/aww/p1",
            subreddit_name="aww",
            discord_channel_id="111",
        )

        self.assertTrue(db.has_been_posted("p1"))
        self.assertTrue(db.has_been_processed("p1", "111"))
        self.assertFalse(db.has_been_processed("p1", "222"))

        # Upsert on the same post+channel must not raise.
        db.mark_as_posted(
            post_id="p1",
            title="First updated",
            reddit_url="https://reddit.com/r/aww/p1",
            subreddit_name="aww",
            discord_channel_id="111",
        )
        self.assertTrue(db.has_been_processed("p1", "111"))

        # Same Reddit post can be recorded for a different channel.
        db.mark_as_posted(
            post_id="p1",
            title="First",
            reddit_url="https://reddit.com/r/aww/p1",
            subreddit_name="aww",
            discord_channel_id="222",
        )
        self.assertTrue(db.has_been_processed("p1", "222"))

    def test_skip_functions(self):
        db.mark_as_skipped(
            post_id="p2",
            title="Skip me",
            reddit_url="https://reddit.com/r/aww/p2",
            subreddit_name="aww",
            discord_channel_id="111",
            reason="no media",
        )
        self.assertTrue(db.has_been_processed("p2", "111"))
        self.assertTrue(db.has_been_posted("p2"))

        with db.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, skip_reason
                    FROM posted_posts
                    WHERE reddit_post_id = %s AND discord_channel_id = %s
                    """,
                    ("p2", "111"),
                )
                status, reason = cursor.fetchone()

        self.assertEqual(status, "skipped")
        self.assertEqual(reason, "no media")

        # Later posting the same item should overwrite the skip.
        db.mark_as_posted(
            post_id="p2",
            title="Skip me",
            reddit_url="https://reddit.com/r/aww/p2",
            subreddit_name="aww",
            discord_channel_id="111",
        )
        with db.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, skip_reason
                    FROM posted_posts
                    WHERE reddit_post_id = %s AND discord_channel_id = %s
                    """,
                    ("p2", "111"),
                )
                status, reason = cursor.fetchone()
        self.assertEqual(status, "posted")
        self.assertIsNone(reason)

    def test_active_carousels_remove_and_clear(self):
        db.upsert_subreddit_config(
            subreddit_name="aww",
            discord_channel_id="111",
            feed_modes="new",
            post_limit=5,
            discord_guild_id="guild-1",
        )
        db.mark_as_posted(
            post_id="p3",
            title="Pic",
            reddit_url="https://reddit.com/r/aww/p3",
            subreddit_name="aww",
            discord_channel_id="111",
        )

        updated = db.set_subreddit_carousels("aww", True, "guild-1")
        self.assertEqual(updated, 1)
        rows = db.get_active_subreddit_configs()
        self.assertTrue(rows[0][5])

        paused = db.set_subreddit_active("aww", False, "guild-1")
        self.assertEqual(paused, 1)
        self.assertEqual(db.get_active_subreddit_configs(), [])
        listed = db.list_subreddit_configs("guild-1")
        self.assertFalse(listed[0][3])

        db.set_subreddit_active("aww", True, "guild-1")
        cleared = db.clear_processed_posts_for_subreddit("aww", "guild-1")
        self.assertEqual(cleared, 1)
        self.assertFalse(db.has_been_processed("p3", "111"))

        removed = db.remove_subreddit_config("aww", "guild-1")
        self.assertEqual(removed, 1)
        self.assertEqual(db.list_subreddit_configs("guild-1"), [])

    def test_default_path_is_local_bot_db_when_no_data_dir(self):
        os.environ.pop("SQLITE_PATH", None)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            previous = Path.cwd()
            os.chdir(cwd)
            try:
                path = db._resolve_sqlite_path()
            finally:
                os.chdir(previous)
        # /data does not exist on this machine, so fallback is ./bot.db
        self.assertTrue(path.endswith("bot.db"))
        self.assertNotEqual(path, "/data/bot.db")


class BotStartupWithoutPostgresTests(unittest.TestCase):
    def test_init_db_and_bot_import_without_database_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "startup.db")
            script = r"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
os.environ.pop("DISCORD_TOKEN", None)
os.environ["SQLITE_PATH"] = %r
sys.path.insert(0, %r)

from db import init_db
init_db()
print("INIT_DB_OK")

import bot
print("BOT_IMPORT_OK")
print("TOKEN_PRESENT", bool(bot.DISCORD_TOKEN))
""" % (db_path, str(REPO_DIR))

            result = subprocess.run(
                [PYTHON, "-c", script],
                cwd=str(REPO_DIR),
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "PYTHONPATH": str(REPO_DIR),
                    "SQLITE_PATH": db_path,
                },
            )

        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        self.assertEqual(
            result.returncode,
            0,
            f"startup script failed:\n{combined}",
        )
        self.assertIn("INIT_DB_OK", result.stdout)
        self.assertIn("BOT_IMPORT_OK", result.stdout)
        self.assertIn("Database: SQLite", result.stdout)
        self.assertIn("TOKEN_PRESENT False", result.stdout)
        self.assertNotIn("DATABASE_URL is missing", combined)
        self.assertNotRegex(combined, r"postgresql://")
        self.assertTrue(Path(db_path).is_file() or "startup.db" in result.stdout)

    def test_bot_py_stops_at_missing_token_not_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "startup.db")
            result = subprocess.run(
                [PYTHON, "bot.py"],
                cwd=str(REPO_DIR),
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "PYTHONPATH": str(REPO_DIR),
                    "SQLITE_PATH": db_path,
                },
            )

        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DISCORD_TOKEN is missing", combined)
        self.assertNotIn("DATABASE_URL is missing", combined)
        self.assertNotIn("psycopg", combined.lower())


if __name__ == "__main__":
    unittest.main()
