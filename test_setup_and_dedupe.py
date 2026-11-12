"""Unit tests for the blocklist, autocomplete parsing, per-channel settings, and dedupe."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("FEED_MODES", "new")
os.environ.setdefault("POST_LIMIT", "10")
os.environ.setdefault("CHECK_INTERVAL_MINUTES", "5")
os.environ["IGNORE_CHANNELS"] = "general,rules,announcements"
os.environ.pop("CHANNEL_SUBREDDIT_MAP", None)
os.environ.pop("DISCORD_TOKEN", None)

import db  # noqa: E402
import reddit_fetcher  # noqa: E402
import subreddit_policy as policy  # noqa: E402
import bot  # noqa: E402


class FakeChannel:
    def __init__(self, channel_id: int, name: str, nsfw: bool, mention=None):
        self.id = channel_id
        self.name = name
        self.nsfw = nsfw
        self.mention = mention or f"<#{channel_id}>"

    def is_nsfw(self) -> bool:
        return self.nsfw


class BlocklistTests(unittest.TestCase):
    def test_blocks_minors_and_nonconsent_only(self):
        blocked = [
            "teen",
            "YoungGirls",
            "loli",
            "shota",
            "jailbait",
            "underage",
            "child",
            "kids",
            "minor",
            "schoolgirl",
            "rape_hentai",
            "noncon",
            "non_con",
            "nonconsent",
            "cnc",
            "forced",
            "drugged",
            "sleeping",
            "unaware",
            "r/teen",
        ]
        for name in blocked:
            self.assertTrue(policy.is_blocked_subreddit(name), name)
            self.assertIsNotNone(policy.blocked_reason(name), name)

        allowed = [
            "bondage",
            "hentai",
            "gore",
            "incest",
            "aww",
            "pics",
            "NSFW_GIF",
            "realgirls",
        ]
        for name in allowed:
            self.assertFalse(policy.is_blocked_subreddit(name), name)
            self.assertIsNone(policy.blocked_reason(name), name)

        self.assertEqual(policy.blocked_reason("teen"), "minors-related")
        self.assertEqual(policy.blocked_reason("rape_hentai"), "rape/non-consent")

    def test_parse_and_filter_lists(self):
        names = policy.parse_subreddit_list(" r/aww, bondage, AWW, ,r/teen ")
        self.assertEqual(names, ["aww", "bondage", "teen"])
        allowed, blocked = policy.allowed_subreddit_names(names)
        self.assertEqual(allowed, ["aww", "bondage"])
        self.assertEqual(blocked[0][0], "teen")


class AutocompleteParsingTests(unittest.TestCase):
    def test_split_csv_autocomplete(self):
        self.assertEqual(policy.split_csv_autocomplete("bondage"), ("", "bondage"))
        self.assertEqual(policy.split_csv_autocomplete("aww, bond"), ("aww, ", "bond"))
        self.assertEqual(policy.split_csv_autocomplete("aww,"), ("aww, ", ""))
        self.assertEqual(policy.split_csv_autocomplete("aww,pics, go"), ("aww,pics, ", "go"))
        self.assertEqual(policy.split_csv_autocomplete(""), ("", ""))
        self.assertEqual(policy.split_csv_autocomplete(None), ("", ""))

    def test_format_subscriber_count(self):
        self.assertEqual(policy.format_subscriber_count(733000), "733K")
        self.assertEqual(policy.format_subscriber_count(1500), "1.5K")
        self.assertEqual(policy.format_subscriber_count(1_200_000), "1.2M")
        self.assertEqual(policy.format_subscriber_count(42), "42")
        self.assertEqual(policy.format_subreddit_choice("bondage", 733000), "bondage (733K)")
        self.assertEqual(policy.format_subreddit_choice("aww", None), "aww")

    def test_search_failure_returns_empty(self):
        reddit_fetcher._SEARCH_CACHE.clear()
        with patch("reddit_fetcher.requests.get", side_effect=RuntimeError("offline")):
            self.assertEqual(reddit_fetcher.search_subreddits("bondage"), [])


class MediaDedupeTests(unittest.TestCase):
    def test_preferred_image_urls_collapse_preview_and_full(self):
        urls = [
            "https://preview.redd.it/abc123.jpg?width=320",
            "https://i.redd.it/abc123.jpg",
            "https://preview.redd.it/abc123.jpg?width=640",
        ]
        preferred = reddit_fetcher.preferred_image_urls(urls)
        self.assertEqual(preferred, ["https://i.redd.it/abc123.jpg"])

    def test_media_keys_include_file_ids_and_crosspost_parent(self):
        post = {
            "id": "https://www.reddit.com/r/aww/comments/abc123/title",
            "url": "https://www.reddit.com/r/other/comments/abc123/title",
            "crosspost_parent": "t3_xyz789",
            "image_urls": ["https://i.redd.it/cute01.jpg"],
            "video_urls": [],
        }
        keys = reddit_fetcher.media_keys_for_post(post)
        self.assertIn("post:abc123", keys)
        self.assertIn("post:xyz789", keys)
        self.assertIn("image:cute01", keys)

    def test_same_video_id_is_one_key(self):
        keys = reddit_fetcher.media_keys_for_post(
            {
                "id": "t3_one",
                "url": "https://reddit.com/r/x/comments/one/t",
                "image_urls": [],
                "video_urls": [
                    "https://v.redd.it/vid99",
                    "https://v.redd.it/vid99/DASH_720.mp4",
                ],
            }
        )
        self.assertEqual(keys.count("video:vid99"), 1)


class ChannelScheduleTests(unittest.TestCase):
    def test_channel_is_due_respects_interval(self):
        now = 1_000_000.0
        last = {"111": now - 30}
        self.assertFalse(bot.channel_is_due("111", 1, now=now, last_checked=last))
        last = {"111": now - 61}
        self.assertTrue(bot.channel_is_due("111", 1, now=now, last_checked=last))
        last = {"111": now - 200}
        self.assertFalse(bot.channel_is_due("111", 5, now=now, last_checked=last))
        last = {"111": now - 301}
        self.assertTrue(bot.channel_is_due("111", 5, now=now, last_checked=last))
        # Never checked before: due immediately.
        self.assertTrue(bot.channel_is_due("222", 5, now=now, last_checked={}))


class SetupAndClaimTests(unittest.TestCase):
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

    def test_claim_is_atomic_and_survives_restart(self):
        keys = ["image:cute01", "post:abc123"]
        first = db.claim_post_for_channel(
            post_id="https://reddit.com/r/aww/comments/abc123/t",
            title="Cute",
            reddit_url="https://reddit.com/r/aww/comments/abc123/t",
            subreddit_name="aww",
            discord_channel_id="111",
            media_keys=keys,
        )
        self.assertTrue(first)
        # Same post id in the same channel must lose the race.
        second = db.claim_post_for_channel(
            post_id="https://reddit.com/r/aww/comments/abc123/t",
            title="Cute again",
            reddit_url="https://reddit.com/r/aww/comments/abc123/t",
            subreddit_name="aww",
            discord_channel_id="111",
            media_keys=keys,
        )
        self.assertFalse(second)
        # Same media from a different post id is also blocked.
        third = db.claim_post_for_channel(
            post_id="https://reddit.com/r/pics/comments/zzz999/t",
            title="Crosspost",
            reddit_url="https://reddit.com/r/pics/comments/zzz999/t",
            subreddit_name="pics",
            discord_channel_id="111",
            media_keys=["image:cute01", "post:zzz999"],
        )
        self.assertFalse(third)
        # A different channel can still take the same post.
        other = db.claim_post_for_channel(
            post_id="https://reddit.com/r/aww/comments/abc123/t",
            title="Cute",
            reddit_url="https://reddit.com/r/aww/comments/abc123/t",
            subreddit_name="aww",
            discord_channel_id="222",
            media_keys=keys,
        )
        self.assertTrue(other)

        db.mark_as_posted(
            post_id="https://reddit.com/r/aww/comments/abc123/t",
            title="Cute",
            reddit_url="https://reddit.com/r/aww/comments/abc123/t",
            subreddit_name="aww",
            discord_channel_id="111",
        )

        # Restart: same SQLite file, fresh init.
        db._backend_announced = False
        db.init_db()
        self.assertTrue(
            db.already_posted_to_channel(
                "https://reddit.com/r/aww/comments/abc123/t",
                "111",
                keys,
            )
        )
        self.assertTrue(db.has_media_been_processed("image:cute01", "111"))

    def test_release_claim_allows_retry(self):
        keys = ["video:vid99"]
        self.assertTrue(
            db.claim_post_for_channel(
                post_id="p1",
                title="Vid",
                reddit_url="https://reddit.com/r/aww/p1",
                subreddit_name="aww",
                discord_channel_id="111",
                media_keys=keys,
            )
        )
        db.release_claim("p1", "111", keys)
        self.assertFalse(db.already_posted_to_channel("p1", "111", keys))
        self.assertTrue(
            db.claim_post_for_channel(
                post_id="p1",
                title="Vid",
                reddit_url="https://reddit.com/r/aww/p1",
                subreddit_name="aww",
                discord_channel_id="111",
                media_keys=keys,
            )
        )

    def test_setup_saves_per_channel_settings_and_blocks_bad_subs(self):
        nsfw = FakeChannel(111, "aww", True)
        sfw = FakeChannel(222, "general", False)

        ok, error, _summary = bot.apply_setup_settings(
            discord_guild_id="guild-1",
            target_channel=sfw,
            subreddits_text="aww",
            interval_minutes=2,
        )
        self.assertFalse(ok)
        self.assertIn("age-restricted", error)

        ok, warning, summary = bot.apply_setup_settings(
            discord_guild_id="guild-1",
            target_channel=nsfw,
            subreddits_text="aww, bondage, teen, rape_hentai",
            interval_minutes=2,
            media_type="videos",
            timerange="hot",
        )
        self.assertTrue(ok)
        self.assertEqual(summary["subreddits"], ["aww", "bondage"])
        self.assertEqual(summary["interval_minutes"], 2)
        self.assertEqual(summary["media_type"], "videos")
        self.assertEqual(summary["timerange"], "hot")
        self.assertIsNotNone(warning)
        self.assertIn("teen", warning)
        self.assertIn("rape_hentai", warning)

        settings = db.get_channel_settings("111")
        self.assertEqual(settings["interval_minutes"], 2)
        self.assertEqual(settings["media_type"], "videos")
        self.assertEqual(settings["feed_modes"], "hot")

        configs = db.get_active_subreddit_configs()
        names = sorted(row[0] for row in configs if row[1] == "111")
        self.assertEqual(names, ["aww", "bondage"])

        job_settings = bot.get_channel_job_settings("111")
        self.assertEqual(job_settings["interval_minutes"], 2)
        self.assertEqual(job_settings["media_type"], "videos")
        self.assertEqual(job_settings["feed_modes"], ["hot"])

        # Channel with no setup falls back to server-wide defaults.
        fallback = bot.get_channel_job_settings("999")
        self.assertIsNone(fallback["interval_minutes"])
        self.assertIsNone(fallback["media_type"])
        self.assertIsNone(fallback["feed_modes"])

    def test_setup_rejects_all_blocked_and_invalid_interval(self):
        channel = FakeChannel(111, "aww", True)
        ok, error, summary = bot.apply_setup_settings(
            discord_guild_id="guild-1",
            target_channel=channel,
            subreddits_text="teen, rape_hentai",
            interval_minutes=2,
        )
        self.assertFalse(ok)
        self.assertIsNone(summary)
        self.assertIn("can't add", error)

        ok, error, summary = bot.apply_setup_settings(
            discord_guild_id="guild-1",
            target_channel=channel,
            subreddits_text="aww",
            interval_minutes=7,
        )
        self.assertFalse(ok)
        self.assertIn("1, 2, or 5", error)


if __name__ == "__main__":
    unittest.main()
