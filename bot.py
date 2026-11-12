import asyncio
import io
import mimetypes
import os
import tempfile
import subprocess
import shutil
import re
from urllib.parse import urlparse

import requests
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from db import (
    init_db,
    has_been_processed,
    mark_as_posted,
    #mark_as_skipped,
    get_active_subreddit_configs,
    list_subreddit_configs,
    upsert_subreddit_config,
    remove_subreddit_config,
    set_subreddit_active,
    clear_processed_posts_for_subreddit,
    set_subreddit_carousels,
)
from reddit_fetcher import fetch_latest_posts
from channel_map import (
    DEFAULT_IGNORE_CHANNELS,
    is_ignored_channel,
    parse_csv_names,
    parse_override_map,
    subreddit_candidates,
)


load_dotenv(override=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
#DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

POST_LIMIT = int(os.getenv("POST_LIMIT", 25))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", 5))
MAX_MEDIA_UPLOAD_BYTES = int(os.getenv("MAX_MEDIA_UPLOAD_BYTES", str(8 * 1024 * 1024)))
INCLUDE_CAROUSELS = os.getenv("INCLUDE_CAROUSELS", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

FEED_MODES = [
    mode.strip().lower()
    for mode in os.getenv("FEED_MODES", "old,hot,new").split(",")
    if mode.strip()
]

IGNORE_CHANNELS = parse_csv_names(
    os.getenv("IGNORE_CHANNELS"),
    defaults=DEFAULT_IGNORE_CHANNELS,
)
CHANNEL_SUBREDDIT_OVERRIDES = parse_override_map(
    os.getenv("CHANNEL_SUBREDDIT_MAP", "")
)

rotation_pointer = 0

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
slash_commands_synced = False

async def sync_slash_commands_to_all_guilds():
    total_synced = 0

    if not client.guilds:
        print("Bot is not in any guilds yet. No slash commands synced.")
        return

    for guild in client.guilds:
        guild_object = discord.Object(id=guild.id)

        tree.copy_global_to(guild=guild_object)
        synced = await tree.sync(guild=guild_object)

        total_synced += len(synced)

        print(
            f"Synced {len(synced)} slash commands "
            f"to guild {guild.name} ({guild.id})."
        )

    print(f"Total slash command sync count: {total_synced}")

@client.event
async def on_ready():
    global slash_commands_synced

    print(f"Logged in as {client.user}")

    init_db()

    if not slash_commands_synced:
        await sync_slash_commands_to_all_guilds()
        slash_commands_synced = True

    print(f"Feed modes: {FEED_MODES}")
    print(f"Check interval: {CHECK_INTERVAL_MINUTES} minutes")
    print(f"Post candidate limit: {POST_LIMIT}")
    print(f"Ignored channels: {sorted(IGNORE_CHANNELS) or '(none)'}")
    print(f"Channel overrides: {CHANNEL_SUBREDDIT_OVERRIDES or '(none)'}")
    print("Safety: posting only into age-restricted (nsfw) channels.")

    if not check_reddit_posts.is_running():
        check_reddit_posts.start()
    



def is_carousel_post(post: dict) -> bool:
    """
    Treat a post as a carousel when:

    1. Reddit exposed more than one image, or
    2. The existing media detector flagged it as a possible gallery.
    """
    image_urls = post.get("image_urls", [])
    media_hint = post.get("media_hint", "")

    # Reddit RSS lists the same picture twice (preview.redd.it thumbnail
    # + full i.redd.it file). Count unique image ids, not raw URLs.
    unique_ids = {
        os.path.splitext(urlparse(u).path.rsplit("/", 1)[-1])[0]
        for u in image_urls
        if "external-preview" not in u
    }

    return (
        len(unique_ids) > 1
        or media_hint == "possible_gallery"
    )


DIRECT_VIDEO_EXTENSIONS = {
        ".mp4",
        ".webm",
        ".mov",
    }

def is_redgifs_watch_url(url: str) -> bool:
    parsed_url = urlparse(url)

    return (
        parsed_url.netloc.lower()
        in {"redgifs.com", "www.redgifs.com"}
        and parsed_url.path.lower().startswith("/watch/")
    )

def normalize_redgifs_embed_url(url: str) -> str | None:
    """
    Accept both RedGIFs URL formats:

    https://www.redgifs.com/watch/example
    https://www.redgifs.com/ifr/example

    Returns a normalized /watch/ URL that Discord can preview.
    """
    parsed_url = urlparse(url)

    if parsed_url.netloc.lower() not in {
        "redgifs.com",
        "www.redgifs.com",
    }:
        return None

    path_parts = [
        part
        for part in parsed_url.path.split("/")
        if part
    ]

    if len(path_parts) < 2:
        return None

    url_type = path_parts[0].lower()
    media_id = path_parts[1]

    if url_type not in {"watch", "ifr"}:
        return None

    if not media_id:
        return None

    return f"https://www.redgifs.com/watch/{media_id}"


def _ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def download_reddit_video(video_url: str):
    """
    Reddit videos (v.redd.it) are not direct files. Read the DASH playlist,
    pick the best video quality that fits Discord's upload limit, and merge
    the audio track with ffmpeg when one exists.
    """
    headers = {"User-Agent": "RedditDiscordBot/1.0 (RSS client)"}
    parsed = urlparse(video_url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    base = f"https://v.redd.it/{parts[0]}"

    try:
        mpd = requests.get(f"{base}/DASHPlaylist.mpd", headers=headers, timeout=20)
        mpd.raise_for_status()
    except requests.RequestException as error:
        print(f"Failed to read Reddit video playlist {base}: {error}")
        return None

    names = re.findall(r"<BaseURL>([^<]+)</BaseURL>", mpd.text)
    audio_names = [n for n in names if "AUDIO" in n.upper()]
    video_names = [n for n in names if "AUDIO" not in n.upper()]

    def quality(name):
        m = re.search(r"_(\d+)", name)
        return int(m.group(1)) if m else 0

    video_names.sort(key=quality, reverse=True)
    audio_names.sort(key=quality, reverse=True)

    def fetch(name):
        try:
            r = requests.get(f"{base}/{name}", headers=headers, timeout=60)
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            return None

    ffmpeg = _ffmpeg_path()
    audio_tracks = []
    if ffmpeg:
        for name in audio_names:
            data = fetch(name)
            if data:
                audio_tracks.append(data)

    def merge(video, audio):
        with tempfile.TemporaryDirectory() as tmp:
            v_path = os.path.join(tmp, "v.mp4")
            a_path = os.path.join(tmp, "a.mp4")
            out_path = os.path.join(tmp, "out.mp4")
            with open(v_path, "wb") as f:
                f.write(video)
            with open(a_path, "wb") as f:
                f.write(audio)
            result = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", v_path,
                 "-i", a_path, "-c", "copy", "-map", "0:v:0",
                 "-map", "1:a:0?", "-movflags", "+faststart", out_path],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    return f.read()
        return None

    silent_fallback = None

    # Prefer sound: try every video quality with every audio quality
    # (best first) and only post a silent video if nothing with sound fits.
    for name in video_names:
        video = fetch(name)
        if not video or len(video) > MAX_MEDIA_UPLOAD_BYTES:
            continue

        for audio in audio_tracks:
            merged = merge(video, audio)
            if merged and len(merged) <= MAX_MEDIA_UPLOAD_BYTES:
                return merged, "reddit_video.mp4"

        if not audio_tracks:
            return video, "reddit_video.mp4"

        if silent_fallback is None:
            silent_fallback = video

    if silent_fallback is not None:
        print(f"Posting Reddit video without sound (too large with audio): {video_url}")
        return silent_fallback, "reddit_video.mp4"

    print(f"No Reddit video quality fits the upload limit: {video_url}")
    return None


def download_direct_video(video_url: str):
    """
    Download a direct video URL into memory.

    Returns:
        tuple[bytes, str] when successful
        None when the URL is not a downloadable video or is too large
    """
    headers = {
        "User-Agent": "RedditDiscordBot/1.0 (RSS client; +https://github.com/bytedelay/reddit-discord-bot)"
    }

    try:
        with requests.get(
            video_url,
            headers=headers,
            stream=True,
            timeout=30,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()

            content_type = (
                response.headers
                .get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            final_url = response.url
            path = urlparse(final_url).path
            extension = os.path.splitext(path)[1].lower()

            is_video_content = content_type.startswith("video/")
            has_video_extension = extension in DIRECT_VIDEO_EXTENSIONS

            # Some video hosts return an HTML webpage instead of a video file.
            if not is_video_content and not has_video_extension:
                print(
                    f"Skipping non-direct video URL: {video_url} "
                    f"(Content-Type: {content_type or 'unknown'})"
                )
                return None

            content_length = response.headers.get("Content-Length")

            if content_length and content_length.isdigit():
                if int(content_length) > MAX_MEDIA_UPLOAD_BYTES:
                    print(
                        f"Skipping oversized video: {video_url} "
                        f"({content_length} bytes)"
                    )
                    return None

            data = bytearray()

            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue

                data.extend(chunk)

                if len(data) > MAX_MEDIA_UPLOAD_BYTES:
                    print(
                        f"Skipping oversized video while downloading: "
                        f"{video_url}"
                    )
                    return None

            if not data:
                print(f"Downloaded video was empty: {video_url}")
                return None

            if extension not in DIRECT_VIDEO_EXTENSIONS:
                guessed_extension = mimetypes.guess_extension(content_type)
                extension = guessed_extension or ".mp4"

            filename = f"reddit_video{extension}"

            return bytes(data), filename

    except requests.RequestException as error:
        print(f"Failed to download video {video_url}: {error}")
        return None


async def send_video_as_attachment(
    channel,
    video_url: str,
) -> bool:
    """
    RedGIFs pages are sent directly so Discord creates its native preview.

    Direct video files such as .mp4 and .webm continue through the existing
    download-and-upload function.
    """
    redgifs_embed_url = normalize_redgifs_embed_url(video_url)

    if redgifs_embed_url:
        try:
            await channel.send(redgifs_embed_url)
            return True

        except discord.HTTPException as error:
            print(
                f"Failed to send RedGIFs preview "
                f"{redgifs_embed_url}: {error}"
            )
            return False

    if urlparse(video_url).netloc.lower() == "v.redd.it":
        result = await asyncio.to_thread(download_reddit_video, video_url)
    else:
        result = await asyncio.to_thread(
            download_direct_video,
            video_url,
        )

    if result is None:
        return False

    video_bytes, filename = result

    video_file = discord.File(
        fp=io.BytesIO(video_bytes),
        filename=filename,
    )

    await channel.send(file=video_file)

    return True


async def send_reddit_post_to_discord(
    channel,
    post: dict,
) -> bool:
    """
    Send only the actual media.

    Images:
        Blank embeds containing only the image.

    Videos:
        Uploaded as attachments with no text.

    Returns True when at least one media item was sent.
    """
    image_urls = post.get("image_urls", [])
    video_urls = post.get("video_urls", [])

    media_sent = False

    # Videos first. When a video uploads, skip its thumbnail images so
    # the channel does not get a duplicate preview picture.
    for video_url in video_urls:
        try:
            video_was_sent = await send_video_as_attachment(
                channel=channel,
                video_url=video_url,
            )

            if video_was_sent:
                media_sent = True

        except discord.HTTPException as error:
            print(f"Failed to upload video {video_url}: {error}")

    if media_sent:
        return True

    for image_url in image_urls:
        try:
            embed = discord.Embed()
            embed.set_image(url=image_url)

            await channel.send(embed=embed)
            media_sent = True

        except discord.HTTPException as error:
            print(f"Failed to send image embed {image_url}: {error}")

    return media_sent

def channel_is_age_restricted(channel) -> bool:
    """Return True only when the Discord channel is marked age-restricted."""
    if channel is None:
        return False

    is_nsfw = getattr(channel, "is_nsfw", None)
    if callable(is_nsfw):
        try:
            return bool(is_nsfw())
        except Exception:
            pass

    return bool(getattr(channel, "nsfw", False))


def log_skip_non_nsfw(channel, guild=None) -> None:
    guild_obj = guild or getattr(channel, "guild", None)
    if guild_obj is not None:
        guild_label = f" in guild {guild_obj.name} ({guild_obj.id})"
    else:
        guild_label = ""

    name = getattr(channel, "name", "?")
    channel_id = getattr(channel, "id", "?")
    print(
        f"Skipping #{name} ({channel_id}){guild_label}: "
        "channel is not age-restricted (nsfw). "
        "The bot only posts into age-restricted channels."
    )


def parse_feed_modes(feed_modes_text: str | None) -> list[str]:
    if not feed_modes_text:
        return list(FEED_MODES)

    modes = [
        mode.strip().lower()
        for mode in feed_modes_text.split(",")
        if mode.strip()
    ]
    return modes or list(FEED_MODES)


def load_db_configs_by_channel() -> dict:
    configs = {}

    for row in get_active_subreddit_configs():
        (
            subreddit_name,
            discord_channel_id,
            feed_modes_text,
            config_post_limit,
            discord_guild_id,
            include_carousels,
        ) = row
        configs[str(discord_channel_id)] = {
            "subreddit_name": subreddit_name,
            "feed_modes": parse_feed_modes(feed_modes_text),
            "post_limit": config_post_limit,
            "discord_guild_id": discord_guild_id,
            "include_carousels": include_carousels,
        }

    return configs


def append_jobs(
    jobs: list,
    *,
    candidates: list[str],
    channel,
    discord_channel_id: str,
    discord_guild_id: str,
    feed_modes: list[str],
    post_limit: int,
    include_carousels: bool,
) -> None:
    for feed_mode in feed_modes:
        jobs.append(
            {
                "subreddit_candidates": candidates,
                "discord_channel_id": discord_channel_id,
                "discord_guild_id": discord_guild_id,
                "feed_mode": feed_mode,
                "post_limit": post_limit,
                "include_carousels": include_carousels,
                "channel": channel,
            }
        )


def collect_channel_jobs() -> list:
    """
    Build jobs by reading each guild's text channels.

    Channel #xyz maps to r/xyz (and r/xyz with '-' -> '_' if needed).
    Optional CHANNEL_SUBREDDIT_MAP and saved slash-command configs override
    the name. Ignored channels are skipped unless overridden.
    Non-age-restricted channels are always skipped.
    """
    jobs = []
    db_by_channel = load_db_configs_by_channel()
    handled_channel_ids = set()

    for guild in client.guilds:
        for channel in guild.text_channels:
            channel_id = str(channel.id)
            channel_name = channel.name or ""
            db_config = db_by_channel.get(channel_id)
            has_env_override = channel_name.strip().lower() in CHANNEL_SUBREDDIT_OVERRIDES

            if (
                db_config is None
                and not has_env_override
                and is_ignored_channel(
                    channel_name,
                    IGNORE_CHANNELS,
                    CHANNEL_SUBREDDIT_OVERRIDES,
                )
            ):
                print(
                    f"Ignoring channel #{channel_name} ({channel.id}) "
                    f"in guild {guild.name}."
                )
                continue

            if not channel_is_age_restricted(channel):
                log_skip_non_nsfw(channel, guild)
                continue

            if db_config:
                candidates = [db_config["subreddit_name"]]
                feed_modes = db_config["feed_modes"]
                post_limit = db_config["post_limit"]
                include_carousels = db_config["include_carousels"]
            else:
                candidates = subreddit_candidates(
                    channel_name,
                    CHANNEL_SUBREDDIT_OVERRIDES,
                )
                feed_modes = list(FEED_MODES)
                post_limit = POST_LIMIT
                include_carousels = INCLUDE_CAROUSELS

            if not candidates:
                continue

            handled_channel_ids.add(channel_id)
            append_jobs(
                jobs,
                candidates=candidates,
                channel=channel,
                discord_channel_id=channel_id,
                discord_guild_id=str(guild.id),
                feed_modes=feed_modes,
                post_limit=post_limit,
                include_carousels=include_carousels,
            )

    for channel_id, db_config in db_by_channel.items():
        if channel_id in handled_channel_ids:
            continue

        append_jobs(
            jobs,
            candidates=[db_config["subreddit_name"]],
            channel=None,
            discord_channel_id=channel_id,
            discord_guild_id=str(db_config["discord_guild_id"] or ""),
            feed_modes=db_config["feed_modes"],
            post_limit=db_config["post_limit"],
            include_carousels=db_config["include_carousels"],
        )

    return jobs


def fetch_posts_for_candidates(candidates: list[str], feed_mode: str, limit: int):
    last_name = candidates[-1] if candidates else ""

    for name in candidates:
        last_name = name
        try:
            posts = fetch_latest_posts(
                subreddit_name=name,
                feed_mode=feed_mode,
                limit=limit,
            )
        except Exception as error:
            print(f"Error fetching r/{name} [{feed_mode}]: {error}")
            posts = []

        if posts:
            return name, posts

        print(f"No posts found for r/{name} [{feed_mode}].")

    return last_name, []


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_reddit_posts():
    global rotation_pointer

    print("Checking guild channels for subreddit auto-mapping...")

    jobs = collect_channel_jobs()

    if not jobs:
        print("No eligible age-restricted channel mappings found.")
        return

    start_index = rotation_pointer % len(jobs)

    # Try every channel/mode pair until we find one unposted post.
    for offset in range(len(jobs)):
        job_index = (start_index + offset) % len(jobs)
        job = jobs[job_index]

        candidates = job["subreddit_candidates"]
        include_carousels = job["include_carousels"]
        discord_channel_id = job["discord_channel_id"]
        discord_guild_id = job["discord_guild_id"]
        feed_mode = job["feed_mode"]
        job_post_limit = job["post_limit"]
        candidate_label = " / ".join(f"r/{name}" for name in candidates)

        print(
            f"Trying {candidate_label} [{feed_mode}] "
            f"for guild {discord_guild_id}, channel {discord_channel_id}..."
        )

        try:
            channel = job.get("channel")

            if channel is None:
                channel_id = int(discord_channel_id)
                channel = client.get_channel(channel_id)

                if channel is None:
                    channel = await client.fetch_channel(channel_id)

            if not channel_is_age_restricted(channel):
                log_skip_non_nsfw(channel)
                continue

            subreddit_name, posts = fetch_posts_for_candidates(
                candidates=candidates,
                feed_mode=feed_mode,
                limit=job_post_limit,
            )

            if not posts:
                continue

            for post in posts:
                post_id = post["id"]
                title = post["title"]
                url = post["url"]
                subreddit = post["subreddit"]

                image_urls = post.get("image_urls", [])
                video_urls = post.get("video_urls", [])

                if has_been_processed(post_id, discord_channel_id):
                    print(f"Already processed, skipping: {title}")
                    continue
                carousel_post = is_carousel_post(post)

                if carousel_post and not include_carousels:
                    print(
                        f"Carousel disabled for r/{subreddit_name}, "
                        f"skipping: {title}"
                    )
                    continue

                # The user requested media only.
                # A detected gallery without direct URLs cannot be posted.
                if not image_urls and not video_urls:
                    print(
                        f"No usable direct media URLs, skipping: {title}"
                    )
                    continue

                # Final safety check immediately before sending.
                if not channel_is_age_restricted(channel):
                    log_skip_non_nsfw(channel)
                    break

                media_was_sent = await send_reddit_post_to_discord(
                    channel=channel,
                    post=post,
                )

                if not media_was_sent:
                    print(
                        f"Media could not be rendered or uploaded, "
                        f"skipping without marking as posted: {title}"
                    )
                    continue

                mark_as_posted(
                    post_id=post_id,
                    title=title,
                    reddit_url=url,
                    subreddit_name=subreddit,
                    discord_channel_id=discord_channel_id,
                )

                print(f"Posted to Discord: {title}")

                # Next cycle starts from the next subreddit/mode pair.
                rotation_pointer = job_index + 1

                # Important: only one Reddit post per cycle.
                return

            print(
                f"All candidate posts were repeats for "
                f"r/{subreddit_name} [{feed_mode}]."
            )

        except Exception as error:
            print(
                f"Error while processing "
                f"{candidate_label} [{feed_mode}]: {error}"
            )

    print("No unposted posts found in any mapped age-restricted channel.")


ALLOWED_FEED_MODES = {"old", "hot", "new", "top", "rising"}


def user_can_manage_bot(interaction: discord.Interaction) -> bool:
    """
    Only allow server admins or users with Manage Server permission
    to control DRBOT.
    """
    if not interaction.guild:
        return False

    permissions = interaction.user.guild_permissions

    return permissions.administrator or permissions.manage_guild


def validate_modes(modes: str) -> tuple[bool, str]:
    """
    Validate comma-separated feed modes like:
    old,hot,new
    hot,new
    top,rising
    """
    mode_list = [
        mode.strip().lower()
        for mode in modes.split(",")
        if mode.strip()
    ]

    if not mode_list:
        return False, "No feed modes provided."

    invalid_modes = [
        mode
        for mode in mode_list
        if mode not in ALLOWED_FEED_MODES
    ]

    if invalid_modes:
        return (
            False,
            f"Invalid mode(s): {', '.join(invalid_modes)}. "
            f"Allowed: {', '.join(sorted(ALLOWED_FEED_MODES))}",
        )

    return True, ",".join(mode_list)


@tree.command(
    name="drbot_add",
    description="Add or update a subreddit feed config for this server.",
)
async def drbot_add(
    interaction: discord.Interaction,
    subreddit: str,
    modes: str = "old,hot,new",
    limit: int = 25,
    channel: Optional[discord.TextChannel] = None,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    if limit < 1 or limit > 100:
        await interaction.response.send_message(
            "Limit must be between 1 and 100.",
            ephemeral=True,
        )
        return

    modes_are_valid, normalized_modes_or_error = validate_modes(modes)

    if not modes_are_valid:
        await interaction.response.send_message(
            normalized_modes_or_error,
            ephemeral=True,
        )
        return

    target_channel = channel or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please use this command in a text channel or provide a text channel.",
            ephemeral=True,
        )
        return

    if not channel_is_age_restricted(target_channel):
        await interaction.response.send_message(
            f"{target_channel.mention} is not age-restricted. "
            "This bot only posts into age-restricted (NSFW) channels.",
            ephemeral=True,
        )
        return

    discord_guild_id = str(interaction.guild_id)
    discord_channel_id = str(target_channel.id)

    upsert_subreddit_config(
        subreddit_name=subreddit,
        discord_channel_id=discord_channel_id,
        discord_guild_id=discord_guild_id,
        feed_modes=normalized_modes_or_error,
        post_limit=limit,
    )

    await interaction.response.send_message(
        f"Added/updated `r/{subreddit}` → {target_channel.mention}\n"
        f"Server ID: `{discord_guild_id}`\n"
        f"Channel ID: `{discord_channel_id}`\n"
        f"Modes: `{normalized_modes_or_error}`\n"
        f"Limit: `{limit}`",
        ephemeral=True,
    )


@tree.command(
    name="drbot_list",
    description="List subreddit feed configs for this server.",
)
async def drbot_list(interaction: discord.Interaction):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    discord_guild_id = str(interaction.guild_id)

    configs = list_subreddit_configs(discord_guild_id)

    if not configs:
        await interaction.response.send_message(
            "No subreddit configs found for this server.",
            ephemeral=True,
        )
        return

    lines = []

    for row in configs:
        (
            config_id,
            subreddit_name,
            discord_channel_id,
            is_active,
            feed_modes,
            post_limit,
            include_carousels,
            created_at,
        ) = row

        status = "ACTIVE" if is_active else "INACTIVE"
        carousel_status = "ON" if include_carousels else "OFF"

        lines.append(
            f"`{config_id}` | r/{subreddit_name} | "
            f"<#{discord_channel_id}> | {status} | "
            f"modes=`{feed_modes}` | limit=`{post_limit}`"
            f"carousels=`{carousel_status}`"
        )

    message = "\n".join(lines)

    if len(message) > 1800:
        message = message[:1800] + "\n...truncated"

    await interaction.response.send_message(
        message,
        ephemeral=True,
    )


@tree.command(
    name="drbot_remove",
    description="Remove a subreddit feed config from this server.",
)
async def drbot_remove(
    interaction: discord.Interaction,
    subreddit: str,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    deleted_count = remove_subreddit_config(
        subreddit_name=subreddit,
        discord_guild_id=str(interaction.guild_id),
    )

    await interaction.response.send_message(
        f"Removed `{deleted_count}` config(s) for `r/{subreddit}` from this server.",
        ephemeral=True,
    )


@tree.command(
    name="drbot_pause",
    description="Pause a subreddit feed in this server without deleting it.",
)
async def drbot_pause(
    interaction: discord.Interaction,
    subreddit: str,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    updated_count = set_subreddit_active(
        subreddit_name=subreddit,
        is_active=False,
        discord_guild_id=str(interaction.guild_id),
    )

    await interaction.response.send_message(
        f"Paused `{updated_count}` config(s) for `r/{subreddit}` in this server.",
        ephemeral=True,
    )


@tree.command(
    name="drbot_resume",
    description="Resume a paused subreddit feed in this server.",
)
async def drbot_resume(
    interaction: discord.Interaction,
    subreddit: str,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    updated_count = set_subreddit_active(
        subreddit_name=subreddit,
        is_active=True,
        discord_guild_id=str(interaction.guild_id),
    )

    await interaction.response.send_message(
        f"Resumed `{updated_count}` config(s) for `r/{subreddit}` in this server.",
        ephemeral=True,
    )


@tree.command(
    name="drbot_clear",
    description="Clear processed post history for a subreddit in this server.",
)
async def drbot_clear(
    interaction: discord.Interaction,
    subreddit: str,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    deleted_count = clear_processed_posts_for_subreddit(
        subreddit_name=subreddit,
        discord_guild_id=str(interaction.guild_id),
    )

    await interaction.response.send_message(
        f"Cleared `{deleted_count}` processed post(s) for `r/{subreddit}` in this server.",
        ephemeral=True,
    )

@tree.command(
    name="drbot_carousels",
    description="Enable or disable carousel posts for a subreddit feed.",
)
@app_commands.describe(
    subreddit="Subreddit config to update",
    enabled="Whether carousel/gallery posts should be included",
)
async def drbot_carousels(
    interaction: discord.Interaction,
    subreddit: str,
    enabled: bool,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    updated_count = set_subreddit_carousels(
        subreddit_name=subreddit,
        include_carousels=enabled,
        discord_guild_id=str(interaction.guild_id),
    )

    if updated_count == 0:
        await interaction.response.send_message(
            f"No saved config was found for `r/{subreddit}` in this server.",
            ephemeral=True,
        )
        return

    status = "enabled" if enabled else "disabled"

    await interaction.response.send_message(
        f"Carousel posts are now **{status}** for `r/{subreddit}`.",
        ephemeral=True,
    )


@tree.command(
    name="drbot_fetch",
    description="Fetch and post one subreddit item immediately without saving config.",
)
async def drbot_fetch(
    interaction: discord.Interaction,
    subreddit: str,
    mode: str = "hot",
    limit: int = 25,
    include_carousels: bool = False,
    channel: Optional[discord.TextChannel] = None,
):
    if not user_can_manage_bot(interaction):
        await interaction.response.send_message(
            "You need Manage Server or Administrator permission to use this.",
            ephemeral=True,
        )
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    mode = mode.strip().lower()

    if mode not in ALLOWED_FEED_MODES:
        await interaction.response.send_message(
            f"Invalid mode `{mode}`. Allowed: {', '.join(sorted(ALLOWED_FEED_MODES))}",
            ephemeral=True,
        )
        return

    if limit < 1 or limit > 100:
        await interaction.response.send_message(
            "Limit must be between 1 and 100.",
            ephemeral=True,
        )
        return

    target_channel = channel or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please use this command in a text channel or provide a text channel.",
            ephemeral=True,
        )
        return

    if not channel_is_age_restricted(target_channel):
        await interaction.response.send_message(
            f"{target_channel.mention} is not age-restricted. "
            "This bot only posts into age-restricted (NSFW) channels.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    target_channel_id = str(target_channel.id)

    posts = fetch_latest_posts(
        subreddit_name=subreddit,
        feed_mode=mode,
        limit=limit,
    )

    if not posts:
        await interaction.followup.send(
            f"No posts found for `r/{subreddit}` using mode `{mode}`.",
            ephemeral=True,
        )
        return

    for post in posts:
        post_id = post["id"]
        title = post["title"]
        url = post["url"]

        image_urls = post.get("image_urls", [])
        video_urls = post.get("video_urls", [])
        if has_been_processed(post_id, target_channel_id):
            continue

        carousel_post = is_carousel_post(post)

        if carousel_post and not include_carousels:
            continue

        if not image_urls and not video_urls:
            continue

        media_was_sent = await send_reddit_post_to_discord(
            channel=target_channel,
            post=post,
        )

        if not media_was_sent:
            continue

        mark_as_posted(
            post_id=post_id,
            title=title,
            reddit_url=url,
            subreddit_name=subreddit,
            discord_channel_id=target_channel_id,
        )

        await interaction.followup.send(
            f"Posted one item from `r/{subreddit}` using `{mode}` "
            f"to {target_channel.mention}.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"No unprocessed media/candidate posts found for `r/{subreddit}` using `{mode}`.",
        ephemeral=True,
    )

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is missing from .env")

    client.run(DISCORD_TOKEN)
