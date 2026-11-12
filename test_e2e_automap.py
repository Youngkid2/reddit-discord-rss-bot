"""Simulated end-to-end test without a real Discord token.

Uses a mock guild/channels and an in-memory dedupe store. The RSS fixture
in testdata/python.new.rss is a real public r/python feed snapshot.
"""

import asyncio
import contextlib
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import feedparser

# Configure bot env before importing bot.py
os.environ.setdefault("FEED_MODES", "new")
os.environ.setdefault("POST_LIMIT", "10")
os.environ.setdefault("CHECK_INTERVAL_MINUTES", "5")
os.environ.setdefault("INCLUDE_CAROUSELS", "true")
os.environ["IGNORE_CHANNELS"] = "general,rules,announcements"
os.environ.pop("CHANNEL_SUBREDDIT_MAP", None)
os.environ.pop("DISCORD_TOKEN", None)

import bot  # noqa: E402
from reddit_fetcher import detect_media_candidate, extract_media_urls, fetch_latest_posts  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "testdata" / "python.new.rss"


def posts_from_rss_text(rss_text: str, limit: int = 10):
    feed = feedparser.parse(rss_text)
    posts = []
    for entry in list(feed.entries[:limit]):
        media = extract_media_urls(entry)
        has_media_candidate, media_hint = detect_media_candidate(entry)
        posts.append(
            {
                "id": entry.id,
                "title": entry.title,
                "url": entry.link,
                "image_urls": media["image_urls"],
                "video_urls": media["video_urls"],
                "has_media_candidate": has_media_candidate,
                "media_hint": media_hint,
                "subreddit": "python",
                "feed_mode": "new",
            }
        )
    return posts


def load_python_posts(limit: int = 10):
    if FIXTURE.exists():
        return posts_from_rss_text(FIXTURE.read_text(), limit=limit)
    return fetch_latest_posts(subreddit_name="python", feed_mode="new", limit=limit)


class FakeChannel:
    def __init__(self, channel_id: int, name: str, nsfw: bool):
        self.id = channel_id
        self.name = name
        self.nsfw = nsfw
        self.guild = None
        self.sent = []

    def is_nsfw(self) -> bool:
        return self.nsfw

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, "kwargs": kwargs})


class FakeGuild:
    def __init__(self, guild_id: int, name: str, channels):
        self.id = guild_id
        self.name = name
        self.text_channels = channels
        for channel in channels:
            channel.guild = self


class FakeClient:
    def __init__(self, guilds):
        self.guilds = guilds
        self._channels = {
            channel.id: channel
            for guild in guilds
            for channel in guild.text_channels
        }

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self._channels.get(channel_id)
        if channel is None:
            raise ValueError(f"unknown channel {channel_id}")
        return channel


class EndToEndAutomapTests(unittest.TestCase):
    def setUp(self):
        self.python = FakeChannel(101, "python", nsfw=True)
        self.askreddit = FakeChannel(102, "askreddit", nsfw=False)
        self.general = FakeChannel(103, "general", nsfw=False)
        self.guild = FakeGuild(1, "Test Guild", [self.python, self.askreddit, self.general])
        self.client = FakeClient([self.guild])
        self.processed = set()
        bot.rotation_pointer = 0
        bot.FEED_MODES = ["new"]
        bot.POST_LIMIT = 10
        bot.INCLUDE_CAROUSELS = True
        bot.IGNORE_CHANNELS = {"general", "rules", "announcements"}
        bot.CHANNEL_SUBREDDIT_OVERRIDES = {}
        bot._channel_last_checked.clear()

    def _db_patches(self):
        def has_been_processed(post_id, discord_channel_id):
            return (post_id, str(discord_channel_id)) in self.processed

        def already_posted_to_channel(post_id, discord_channel_id, media_keys=None):
            return (post_id, str(discord_channel_id)) in self.processed

        def claim_post_for_channel(
            post_id,
            title,
            reddit_url,
            subreddit_name,
            discord_channel_id,
            media_keys=None,
        ):
            key = (post_id, str(discord_channel_id))
            if key in self.processed:
                return False
            self.processed.add(key)
            return True

        def mark_as_posted(post_id, title, reddit_url, subreddit_name, discord_channel_id):
            self.processed.add((post_id, str(discord_channel_id)))

        def release_claim(post_id, discord_channel_id, media_keys=None):
            self.processed.discard((post_id, str(discord_channel_id)))

        return (
            patch.object(bot, "client", self.client),
            patch.object(bot, "get_active_subreddit_configs", lambda: []),
            patch.object(bot, "has_been_processed", has_been_processed),
            patch.object(bot, "already_posted_to_channel", already_posted_to_channel),
            patch.object(bot, "claim_post_for_channel", claim_post_for_channel),
            patch.object(bot, "release_claim", release_claim),
            patch.object(bot, "mark_as_posted", mark_as_posted),
            patch.object(bot, "get_channel_settings", lambda channel_id: None),
        )

    def test_collects_only_nsfw_mapped_channel(self):
        with patch.object(bot, "client", self.client), patch.object(
            bot, "get_active_subreddit_configs", lambda: []
        ), patch.object(bot, "get_channel_settings", lambda channel_id: None):
            jobs = bot.collect_channel_jobs()

        channel_ids = {job["discord_channel_id"] for job in jobs}
        self.assertEqual(channel_ids, {"101"})
        self.assertEqual(jobs[0]["subreddit_candidates"], ["python"])
        self.assertIs(jobs[0]["channel"], self.python)

    def test_real_rss_posts_only_to_nsfw_channel_and_dedupes(self):
        posts = load_python_posts(limit=10)
        self.assertTrue(posts, "Real Reddit RSS for r/python returned no posts")

        # r/python is usually text/link posts. The bot only sends media, so
        # give the real RSS items a placeholder image URL to exercise the
        # send + dedupe path without leaving the requested subreddit.
        for post in posts:
            if not post.get("image_urls") and not post.get("video_urls"):
                post["image_urls"] = ["https://example.com/test-image.jpg"]

        # Feed a single real post so the second poll can only succeed
        # if the bot incorrectly reposts it.
        posts = posts[:1]

        async def fake_send(channel, post):
            await channel.send(post["id"])
            return True

        async def run_two_polls():
            await bot.check_reddit_posts()
            first_python = list(self.python.sent)
            first_ask = list(self.askreddit.sent)
            first_general = list(self.general.sent)
            # Clear the due-clock so the second poll actually runs and
            # has to rely on the dedupe store, not the interval guard.
            bot._channel_last_checked.clear()
            await bot.check_reddit_posts()
            return first_python, first_ask, first_general

        patches = self._db_patches() + (
            patch.object(bot, "fetch_latest_posts", lambda **kwargs: posts),
            patch.object(
                bot,
                "send_reddit_post_to_discord",
                AsyncMock(side_effect=fake_send),
            ),
        )
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            first_python, first_ask, first_general = asyncio.run(run_two_polls())

        self.assertEqual(len(first_python), 1, "first poll should post exactly one item")
        self.assertEqual(self.python.sent, first_python, "second poll must not repost")
        self.assertEqual(first_ask, [])
        self.assertEqual(first_general, [])
        self.assertEqual(self.askreddit.sent, [])
        self.assertEqual(self.general.sent, [])
        self.assertEqual(len(self.processed), 1)


if __name__ == "__main__":
    unittest.main()
