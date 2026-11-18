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


class PostingSpeedTests(EndToEndAutomapTests):
    """New posts must reach Discord on the next check, not one per check."""

    # Don't re-run the inherited tests in this class.
    test_collects_only_nsfw_mapped_channel = None
    test_real_rss_posts_only_to_nsfw_channel_and_dedupes = None

    @staticmethod
    def _fake_posts(subreddit: str, count: int):
        return [
            {
                "id": f"t3_{subreddit}{n}",
                "title": f"{subreddit} {n}",
                "url": f"https://www.reddit.com/r/{subreddit}/comments/{subreddit}{n}/x/",
                "image_urls": [f"https://i.redd.it/{subreddit}{n}.jpg"],
                "video_urls": [],
                "subreddit": subreddit,
                "feed_mode": "new",
            }
            for n in range(count)
        ]

    def _add_nsfw_channel(self, channel_id: int, name: str) -> FakeChannel:
        channel = FakeChannel(channel_id, name, nsfw=True)
        channel.guild = self.guild
        self.guild.text_channels.append(channel)
        self.client = FakeClient([self.guild])
        return channel

    def _run(self, feeds: dict, checks, send=None):
        async def default_send(channel, post):
            await channel.send(post["id"])
            return True

        patches = self._db_patches() + (
            patch.object(
                bot,
                "fetch_latest_posts",
                lambda **kwargs: list(feeds.get(kwargs["subreddit_name"], [])),
            ),
            patch.object(
                bot,
                "send_reddit_post_to_discord",
                AsyncMock(side_effect=send or default_send),
            ),
            patch.object(bot, "current_default_interval", lambda: 5),
            patch.object(bot, "current_media_type", lambda: "both"),
        )
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            asyncio.run(checks())

    def test_one_check_posts_every_new_item_in_every_channel(self):
        aww = self._add_nsfw_channel(104, "aww")
        feeds = {
            "python": self._fake_posts("python", 3),
            "aww": self._fake_posts("aww", 2),
        }
        self._run(feeds, bot.check_reddit_posts)

        self.assertEqual(len(self.python.sent), 3)
        self.assertEqual(len(aww.sent), 2)

    def test_cap_per_check_leaves_the_rest_for_the_next_check(self):
        feeds = {"python": self._fake_posts("python", 5)}

        async def two_checks():
            await bot.check_reddit_posts()
            self.assertEqual(len(self.python.sent), 2)
            bot._channel_last_checked.clear()
            await bot.check_reddit_posts()

        with patch.object(bot, "MAX_POSTS_PER_CYCLE", 2):
            self._run(feeds, two_checks)

        self.assertEqual(len(self.python.sent), 4)

    def test_slow_upload_does_not_skip_the_next_tick(self):
        start = 1_800_000_000.0
        clock = {"t": start}
        feeds = {"python": self._fake_posts("python", 1)}

        async def slow_send(channel, post):
            clock["t"] += 20  # download + upload time
            await channel.send(post["id"])
            return True

        async def two_ticks():
            await bot.check_reddit_posts()
            # A new post appears, and the 5-minute loop ticks again.
            feeds["python"] = self._fake_posts("python", 2)
            clock["t"] = start + 5 * 60
            await bot.check_reddit_posts()

        fake_time = type("FakeTime", (), {"time": staticmethod(lambda: clock["t"])})
        with patch.object(bot, "time", fake_time):
            self._run(feeds, two_ticks, send=slow_send)

        self.assertEqual(len(self.python.sent), 2)

    def test_slow_channel_does_not_hold_up_other_channels(self):
        aww = self._add_nsfw_channel(104, "aww")
        feeds = {
            "python": self._fake_posts("python", 1),
            "aww": self._fake_posts("aww", 1),
        }
        order = []
        events = {}

        async def send(channel, post):
            if channel is self.python:
                # A slow video: it only finishes after the other channel
                # has posted. Checked one by one, this would time out.
                await asyncio.wait_for(events["aww_posted"].wait(), timeout=2)
            await channel.send(post["id"])
            order.append(channel.name)
            if channel is aww:
                events["aww_posted"].set()
            return True

        async def check():
            events["aww_posted"] = asyncio.Event()
            await bot.check_reddit_posts()

        with patch.object(bot, "MAX_PARALLEL_CHANNELS", 3):
            self._run(feeds, check, send=send)

        self.assertEqual(order, ["aww", "python"])

    def test_parallel_channels_respect_the_limit(self):
        self._add_nsfw_channel(104, "aww")
        self._add_nsfw_channel(105, "pics")
        feeds = {
            name: self._fake_posts(name, 1) for name in ("python", "aww", "pics")
        }
        in_flight = {"now": 0, "max": 0, "done": 0}

        async def send(channel, post):
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0.05)
            in_flight["now"] -= 1
            in_flight["done"] += 1
            return True

        with patch.object(bot, "MAX_PARALLEL_CHANNELS", 2):
            self._run(feeds, bot.check_reddit_posts, send=send)

        self.assertEqual(in_flight["done"], 3)
        self.assertEqual(in_flight["max"], 2)


if __name__ == "__main__":
    unittest.main()
