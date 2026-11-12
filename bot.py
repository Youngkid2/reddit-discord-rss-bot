import asyncio
import io
import mimetypes
import os
import tempfile
import subprocess
import shutil
import re
import time
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
    get_check_interval_minutes,
    get_media_type,
    set_check_interval_minutes,
    set_media_type,
    parse_interval,
    post_matches_media_filter,
    ALLOWED_MEDIA_TYPES,
    already_posted_to_channel,
    claim_post_for_channel,
    release_claim,
    get_channel_settings,
    list_channel_settings,
    upsert_channel_settings,
    list_channel_intervals,
    replace_channel_subreddit_configs,
)
from reddit_fetcher import (
    fetch_latest_posts,
    search_subreddits,
    preferred_image_urls,
    preferred_video_urls,
    media_keys_for_post,
    normalize_feed_mode,
    TIMERANGE_CHOICES,
)
from subreddit_policy import (
    parse_subreddit_list,
    is_blocked_subreddit,
    blocked_reason,
    allowed_subreddit_names,
    split_csv_autocomplete,
    format_subreddit_choice,
    blocked_refusal_message,
    is_valid_subreddit_name,
    normalize_subreddit_name,
)
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
MAX_MEDIA_UPLOAD_BYTES = int(os.getenv("MAX_MEDIA_UPLOAD_BYTES", str(10 * 1000 * 1000 - 200 * 1000)))
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
_poll_lock = asyncio.Lock()
_poll_in_progress = False
_channel_last_checked: dict[str, float] = {}

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

    effective_interval = CHECK_INTERVAL_MINUTES
    effective_media = "both"
    try:
        effective_interval = get_check_interval_minutes(default=CHECK_INTERVAL_MINUTES)
        effective_media = get_media_type()
    except Exception as error:
        print(f"Failed to load saved settings, using defaults: {error}")

    apply_effective_check_interval(fallback=effective_interval)

    print(f"Feed modes: {FEED_MODES}")
    print(f"Check interval: {effective_interval} minutes")
    print(f"Media type: {effective_media}")
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

    def shrink_same_size(video, audio):
        """Re-encode at the same resolution with sound, small enough to upload."""
        with tempfile.TemporaryDirectory() as tmp:
            v_path = os.path.join(tmp, "v.mp4")
            a_path = os.path.join(tmp, "a.mp4")
            out_path = os.path.join(tmp, "out.mp4")
            with open(v_path, "wb") as f:
                f.write(video)
            with open(a_path, "wb") as f:
                f.write(audio)
            probe = subprocess.run(
                [ffmpeg, "-i", v_path], capture_output=True, text=True
            )
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
            if not m:
                return None
            seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if seconds <= 0:
                return None
            total_kbit = MAX_MEDIA_UPLOAD_BYTES * 8 / 1000 * 0.92
            video_kbps = int(total_kbit / seconds) - 96
            if video_kbps < 300:
                return None
            result = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", v_path, "-i", a_path,
                 "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "libx264",
                 "-preset", "veryfast", "-b:v", f"{video_kbps}k",
                 "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
                 "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_path],
                capture_output=True,
                timeout=600,
            )
            if result.returncode == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    data = f.read()
                if len(data) <= MAX_MEDIA_UPLOAD_BYTES:
                    return data
        return None

    # Best quality first: if the top quality with sound is too big, keep the
    # same resolution and compress it instead of dropping to a smaller size.
    if video_names and audio_tracks:
        top = fetch(video_names[0])
        if top:
            merged = merge(top, audio_tracks[0])
            if merged and len(merged) <= MAX_MEDIA_UPLOAD_BYTES:
                return merged, "reddit_video.mp4"
            if len(top) <= 60 * 1000 * 1000:
                shrunk = shrink_same_size(top, audio_tracks[0])
                if shrunk:
                    return shrunk, "reddit_video.mp4"

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


def download_redgifs_video(video_url: str):
    """
    Download a RedGIFs video file so we can upload it without a blue link.

    Returns (bytes, filename) or None.
    """
    embed_url = normalize_redgifs_embed_url(video_url) or video_url
    parsed = urlparse(embed_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    gif_id = parts[1]
    if not gif_id:
        return None

    headers = {"User-Agent": "RedditDiscordBot/1.0 (RSS client)"}
    try:
        auth = requests.get(
            "https://api.redgifs.com/v2/auth/temporary",
            headers=headers,
            timeout=15,
        )
        auth.raise_for_status()
        auth_payload = auth.json()
        token = (
            auth_payload.get("token")
            or auth_payload.get("access_token")
            or (auth_payload.get("data") or {}).get("token")
        )
        if not token:
            print(f"RedGIFs auth did not return a token for {gif_id}")
            return None

        info_headers = {
            "User-Agent": headers["User-Agent"],
            "Authorization": f"Bearer {token}",
        }
        info = requests.get(
            f"https://api.redgifs.com/v2/gifs/{gif_id}",
            headers=info_headers,
            timeout=15,
        )
        info.raise_for_status()
        urls = ((info.json().get("gif") or {}).get("urls") or {})
        for key in ("hd", "sd", "html"):
            file_url = urls.get(key)
            if not file_url or key == "html":
                continue
            downloaded = download_direct_video(file_url)
            if downloaded:
                data, filename = downloaded
                if not filename.lower().endswith((".mp4", ".webm", ".mov")):
                    filename = "redgifs_video.mp4"
                return data, filename
    except requests.RequestException as error:
        print(f"Failed to download RedGIFs video {video_url}: {error}")
        return None

    print(f"No RedGIFs file fit the upload limit: {video_url}")
    return None


async def send_video_as_attachment(
    channel,
    video_url: str,
) -> bool:
    """
    Upload the video file with no message text, so Discord does not show
    a blue Reddit or RedGIFs link.

    Direct video files such as .mp4 and .webm are downloaded and attached.
    Reddit hosted videos go through the DASH merge path. RedGIFs pages are
    downloaded as files when possible; a raw URL is never sent.
    """
    redgifs_embed_url = normalize_redgifs_embed_url(video_url)
    host = urlparse(video_url).netloc.lower()

    if redgifs_embed_url or "redgifs.com" in host:
        result = await asyncio.to_thread(download_redgifs_video, video_url)
        if result is None:
            print(
                f"Skipping RedGIFs URL without uploading a file "
                f"(links disabled): {video_url}"
            )
            return False
        video_bytes, filename = result
        video_file = discord.File(
            fp=io.BytesIO(video_bytes),
            filename=filename,
        )
        await channel.send(file=video_file)
        return True

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
    image_urls = preferred_image_urls(post.get("image_urls", []))
    video_urls = preferred_video_urls(post.get("video_urls", []))

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

            await channel.send(content=None, embed=embed)
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
    """Return {channel_id: [config, ...]} so a channel can have many subreddits."""
    configs: dict[str, list[dict]] = {}

    for row in get_active_subreddit_configs():
        (
            subreddit_name,
            discord_channel_id,
            feed_modes_text,
            config_post_limit,
            discord_guild_id,
            include_carousels,
        ) = row
        channel_id = str(discord_channel_id)
        configs.setdefault(channel_id, []).append(
            {
                "subreddit_name": subreddit_name,
                "feed_modes": parse_feed_modes(feed_modes_text),
                "post_limit": config_post_limit,
                "discord_guild_id": discord_guild_id,
                "include_carousels": include_carousels,
            }
        )

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
    interval_minutes: int | None = None,
    media_type: str | None = None,
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
                "interval_minutes": interval_minutes,
                "media_type": media_type,
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

            channel_settings = get_channel_job_settings(channel_id)
            interval_minutes = channel_settings["interval_minutes"]
            media_type = channel_settings["media_type"]
            settings_feed_modes = channel_settings["feed_modes"]

            if db_config:
                for item in db_config:
                    name = item["subreddit_name"]
                    if is_blocked_subreddit(name):
                        print(
                            f"Skipping blocked subreddit r/{name} "
                            f"mapped to #{channel_name} ({channel.id})."
                        )
                        continue
                    append_jobs(
                        jobs,
                        candidates=[name],
                        channel=channel,
                        discord_channel_id=channel_id,
                        discord_guild_id=str(guild.id),
                        feed_modes=settings_feed_modes or item["feed_modes"],
                        post_limit=item["post_limit"],
                        include_carousels=item["include_carousels"],
                        interval_minutes=interval_minutes,
                        media_type=media_type,
                    )
                    handled_channel_ids.add(channel_id)
                continue

            candidates = [
                name
                for name in subreddit_candidates(
                    channel_name,
                    CHANNEL_SUBREDDIT_OVERRIDES,
                )
                if not is_blocked_subreddit(name)
            ]
            if not candidates:
                if any(
                    is_blocked_subreddit(name)
                    for name in subreddit_candidates(
                        channel_name,
                        CHANNEL_SUBREDDIT_OVERRIDES,
                    )
                ):
                    print(
                        f"Skipping #{channel_name} ({channel.id}): "
                        "the auto-mapped subreddit is blocked."
                    )
                continue

            handled_channel_ids.add(channel_id)
            append_jobs(
                jobs,
                candidates=candidates,
                channel=channel,
                discord_channel_id=channel_id,
                discord_guild_id=str(guild.id),
                feed_modes=settings_feed_modes or list(FEED_MODES),
                post_limit=POST_LIMIT,
                include_carousels=INCLUDE_CAROUSELS,
                interval_minutes=interval_minutes,
                media_type=media_type,
            )

    for channel_id, db_config in db_by_channel.items():
        if channel_id in handled_channel_ids:
            continue

        channel_settings = get_channel_job_settings(channel_id)
        for item in db_config:
            name = item["subreddit_name"]
            if is_blocked_subreddit(name):
                print(
                    f"Skipping blocked subreddit r/{name} "
                    f"mapped to channel {channel_id}."
                )
                continue
            append_jobs(
                jobs,
                candidates=[name],
                channel=None,
                discord_channel_id=channel_id,
                discord_guild_id=str(item["discord_guild_id"] or ""),
                feed_modes=channel_settings["feed_modes"] or item["feed_modes"],
                post_limit=item["post_limit"],
                include_carousels=item["include_carousels"],
                interval_minutes=channel_settings["interval_minutes"],
                media_type=channel_settings["media_type"],
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


def get_channel_job_settings(discord_channel_id: str) -> dict:
    """Per-channel interval/media/feed, falling back to server-wide settings."""
    saved = None
    try:
        saved = get_channel_settings(str(discord_channel_id))
    except Exception as error:
        print(f"Failed to read channel settings for {discord_channel_id}: {error}")

    saved = saved or {}
    interval = parse_interval(saved.get("interval_minutes"))
    media = None
    raw_media = saved.get("media_type")
    if isinstance(raw_media, str) and raw_media.strip().lower() in ALLOWED_MEDIA_TYPES:
        media = raw_media.strip().lower()
    feed_modes = parse_feed_modes(saved.get("feed_modes")) if saved.get("feed_modes") else None
    return {
        "interval_minutes": interval,
        "media_type": media,
        "feed_modes": feed_modes,
    }


def channel_is_due(
    channel_id: str,
    interval_minutes: int | None,
    now: float | None = None,
    last_checked: dict[str, float] | None = None,
) -> bool:
    """Return True if this channel's interval has elapsed since it was last checked."""
    minutes = normalize_interval_for_loop(interval_minutes or current_default_interval())
    clock = time.time() if now is None else now
    history = _channel_last_checked if last_checked is None else last_checked
    last = history.get(str(channel_id), 0)
    return (clock - last) >= (minutes * 60) - 0.5


def current_default_interval() -> int:
    try:
        return get_check_interval_minutes(default=CHECK_INTERVAL_MINUTES)
    except Exception as error:
        print(f"Failed to read default interval, using env value: {error}")
        return CHECK_INTERVAL_MINUTES if CHECK_INTERVAL_MINUTES > 0 else 5


def effective_loop_interval_minutes(fallback: int | None = None) -> int:
    values = [normalize_interval_for_loop(fallback or current_default_interval())]
    try:
        values.extend(list_channel_intervals())
    except Exception as error:
        print(f"Failed to load per-channel intervals: {error}")
    return min(values) if values else 5


def apply_effective_check_interval(fallback: int | None = None) -> bool:
    return apply_check_interval(effective_loop_interval_minutes(fallback=fallback))


def apply_check_interval(minutes: int) -> bool:
    """
    Change the posting-loop interval live. Failures are logged and the
    current interval is left unchanged.
    """
    try:
        minutes = normalize_interval_for_loop(minutes)
        check_reddit_posts.change_interval(minutes=minutes)
        return True
    except Exception as error:
        print(f"Failed to apply check interval, keeping current value: {error}")
        return False


def normalize_interval_for_loop(minutes: int) -> int:
    parsed = parse_interval(minutes)
    if parsed is not None:
        return parsed
    try:
        fallback = int(minutes)
        if fallback > 0:
            return fallback
    except (TypeError, ValueError):
        pass
    return CHECK_INTERVAL_MINUTES if CHECK_INTERVAL_MINUTES > 0 else 5


def current_media_type() -> str:
    try:
        return get_media_type()
    except Exception as error:
        print(f"Failed to read media type, using both: {error}")
        return "both"


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_reddit_posts():
    global _poll_in_progress

    async with _poll_lock:
        if _poll_in_progress:
            print("Skipping poll cycle: previous cycle is still running.")
            return
        _poll_in_progress = True

    try:
        await run_reddit_post_check()
    finally:
        async with _poll_lock:
            _poll_in_progress = False


async def run_reddit_post_check():
    global rotation_pointer

    print("Checking guild channels for subreddit auto-mapping...")

    jobs = collect_channel_jobs()

    if not jobs:
        print("No eligible age-restricted channel mappings found.")
        return

    start_index = rotation_pointer % len(jobs)
    default_interval = current_default_interval()
    now = time.time()
    jobs_by_channel: dict[str, list[int]] = {}
    for index, job in enumerate(jobs):
        jobs_by_channel.setdefault(job["discord_channel_id"], []).append(index)
    attempted_indexes: set[int] = set()

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
        interval_minutes = job.get("interval_minutes") or default_interval
        job_media_type = job.get("media_type") or current_media_type()
        candidate_label = " / ".join(f"r/{name}" for name in candidates)

        if not channel_is_due(discord_channel_id, interval_minutes, now=now):
            continue

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
                attempted_indexes.add(job_index)
                continue

            subreddit_name, posts = fetch_posts_for_candidates(
                candidates=candidates,
                feed_mode=feed_mode,
                limit=job_post_limit,
            )

            attempted_indexes.add(job_index)

            if not posts:
                continue

            for post in posts:
                post_id = post["id"]
                title = post["title"]
                url = post["url"]
                subreddit = post["subreddit"]

                image_urls = preferred_image_urls(post.get("image_urls", []))
                video_urls = preferred_video_urls(post.get("video_urls", []))
                media_keys = media_keys_for_post(
                    {
                        **post,
                        "image_urls": image_urls,
                        "video_urls": video_urls,
                    }
                )

                if already_posted_to_channel(post_id, discord_channel_id, media_keys):
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

                try:
                    if not post_matches_media_filter(
                        {"image_urls": image_urls, "video_urls": video_urls},
                        job_media_type,
                    ):
                        print(
                            f"Media filter is {job_media_type}, skipping: {title}"
                        )
                        continue
                except Exception as error:
                    print(
                        f"Media filter failed, posting anyway: {error}"
                    )

                # Final safety check immediately before sending.
                if not channel_is_age_restricted(channel):
                    log_skip_non_nsfw(channel)
                    break

                if not claim_post_for_channel(
                    post_id=post_id,
                    title=title,
                    reddit_url=url,
                    subreddit_name=subreddit,
                    discord_channel_id=discord_channel_id,
                    media_keys=media_keys,
                ):
                    print(f"Already claimed, skipping: {title}")
                    continue

                try:
                    media_was_sent = await send_reddit_post_to_discord(
                        channel=channel,
                        post={
                            **post,
                            "image_urls": image_urls,
                            "video_urls": video_urls,
                        },
                    )
                except Exception:
                    release_claim(post_id, discord_channel_id, media_keys)
                    raise

                if not media_was_sent:
                    release_claim(post_id, discord_channel_id, media_keys)
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
                _channel_last_checked[str(discord_channel_id)] = time.time()

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

    for channel_id, indexes in jobs_by_channel.items():
        if indexes and all(index in attempted_indexes for index in indexes):
            _channel_last_checked[str(channel_id)] = time.time()

    print("No unposted posts found in any mapped age-restricted channel.")


ALLOWED_FEED_MODES = {
    "old",
    "hot",
    "new",
    "top",
    "rising",
    "top-day",
    "top-week",
    "top-month",
    "top-year",
}


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
    mode_list = []
    for mode in modes.split(","):
        raw = mode.strip()
        if not raw:
            continue
        normalized = normalize_feed_mode(raw) or raw.strip().lower()
        mode_list.append(normalized)

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

    subreddit = normalize_subreddit_name(subreddit)
    if not is_valid_subreddit_name(subreddit):
        await interaction.response.send_message(
            "Please enter a valid subreddit name.",
            ephemeral=True,
        )
        return

    if is_blocked_subreddit(subreddit):
        await interaction.response.send_message(
            blocked_refusal_message([(subreddit, blocked_reason(subreddit) or "blocked")]),
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

    subreddit = normalize_subreddit_name(subreddit)
    if not is_valid_subreddit_name(subreddit):
        await interaction.response.send_message(
            "Please enter a valid subreddit name.",
            ephemeral=True,
        )
        return

    if is_blocked_subreddit(subreddit):
        await interaction.response.send_message(
            blocked_refusal_message([(subreddit, blocked_reason(subreddit) or "blocked")]),
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

        image_urls = preferred_image_urls(post.get("image_urls", []))
        video_urls = preferred_video_urls(post.get("video_urls", []))
        media_keys = media_keys_for_post(
            {**post, "image_urls": image_urls, "video_urls": video_urls}
        )
        if already_posted_to_channel(post_id, target_channel_id, media_keys):
            continue

        carousel_post = is_carousel_post(post)

        if carousel_post and not include_carousels:
            continue

        if not image_urls and not video_urls:
            continue

        try:
            if not post_matches_media_filter(
                {"image_urls": image_urls, "video_urls": video_urls},
                current_media_type(),
            ):
                continue
        except Exception:
            pass

        if not claim_post_for_channel(
            post_id=post_id,
            title=title,
            reddit_url=url,
            subreddit_name=subreddit,
            discord_channel_id=target_channel_id,
            media_keys=media_keys,
        ):
            continue

        try:
            media_was_sent = await send_reddit_post_to_discord(
                channel=target_channel,
                post={**post, "image_urls": image_urls, "video_urls": video_urls},
            )
        except Exception:
            release_claim(post_id, target_channel_id, media_keys)
            raise

        if not media_was_sent:
            release_claim(post_id, target_channel_id, media_keys)
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



def apply_setup_settings(
    *,
    discord_guild_id: str,
    target_channel,
    subreddits_text: str,
    interval_minutes: int,
    media_type: str | None = None,
    timerange: str | None = None,
) -> tuple[bool, str | None, dict | None]:
    """
    Apply /setup to one channel.

    Returns (ok, error_message, summary). Used by the slash command and tests.
    """
    if target_channel is None or not channel_is_age_restricted(target_channel):
        mention = getattr(target_channel, "mention", None) or "That channel"
        return (
            False,
            f"{mention} is not age-restricted. "
            "This bot only posts into age-restricted (NSFW) channels. "
            "Turn on Age-Restricted in the channel settings and try again.",
            None,
        )

    names = parse_subreddit_list(subreddits_text)
    if not names:
        return False, "Please enter at least one subreddit name.", None

    invalid = [name for name in names if not is_valid_subreddit_name(name)]
    if invalid:
        return (
            False,
            "These subreddit names are not valid: "
            + ", ".join(f"r/{name}" for name in invalid)
            + ".",
            None,
        )

    allowed, blocked = allowed_subreddit_names(names)
    if blocked and not allowed:
        return False, blocked_refusal_message(blocked), None

    parsed_interval = parse_interval(interval_minutes)
    if parsed_interval is None:
        return False, "Please choose 1, 2, or 5 minutes.", None

    kind = None
    if media_type:
        kind = str(media_type).strip().lower()
        if kind not in ALLOWED_MEDIA_TYPES:
            return False, "Please choose videos, images, or both.", None

    feed_modes = None
    if timerange:
        feed_modes = normalize_feed_mode(timerange)
        if not feed_modes or feed_modes not in ALLOWED_FEED_MODES:
            return (
                False,
                "Please choose a valid timerange such as new, hot, top-day, or top-week.",
                None,
            )

    channel_id = str(target_channel.id)
    replace_channel_subreddit_configs(
        discord_channel_id=channel_id,
        discord_guild_id=str(discord_guild_id),
        subreddit_names=allowed,
        feed_modes=feed_modes or "old,hot,new",
    )
    saved = upsert_channel_settings(
        discord_channel_id=channel_id,
        discord_guild_id=str(discord_guild_id),
        interval_minutes=parsed_interval,
        media_type=kind,
        feed_modes=feed_modes,
        overwrite=True,
    )
    if not saved:
        return (
            False,
            "I could not save the channel settings right now. Please try again.",
            None,
        )

    apply_effective_check_interval(fallback=current_default_interval())

    summary = {
        "channel_id": channel_id,
        "subreddits": allowed,
        "blocked": blocked,
        "interval_minutes": parsed_interval,
        "media_type": kind or current_media_type(),
        "media_was_set": kind is not None,
        "timerange": feed_modes,
        "channel_mention": getattr(target_channel, "mention", f"<#{channel_id}>"),
    }
    warning = blocked_refusal_message(blocked) if blocked else None
    return True, warning, summary


def build_setup_embed(summary: dict, warning: str | None = None) -> discord.Embed:
    subreddit_text = ", ".join(f"r/{name}" for name in summary["subreddits"])
    media_label = {
        "videos": "videos only",
        "images": "images only",
        "both": "both images and videos",
    }.get(summary["media_type"], summary["media_type"])
    if not summary.get("media_was_set"):
        media_label = f"{media_label} (server default)"
    timerange = summary.get("timerange") or "server default"

    embed = discord.Embed(
        title="Channel setup saved",
        color=discord.Color.blurple(),
        description=f"The bot will post into {summary['channel_mention']}.",
    )
    embed.add_field(name="Subreddits", value=subreddit_text or "(none)", inline=False)
    embed.add_field(
        name="Interval",
        value=f"{summary['interval_minutes']} minute(s)",
        inline=True,
    )
    embed.add_field(name="Media", value=media_label, inline=True)
    embed.add_field(name="Timerange", value=str(timerange), inline=True)
    if warning:
        embed.add_field(name="Skipped", value=warning, inline=False)
    embed.set_footer(text="The bot only posts into age-restricted channels.")
    return embed


def require_manage_server(interaction: discord.Interaction) -> str | None:
    """Return an error message if the user cannot use admin commands."""
    if not interaction.guild or not interaction.guild_id:
        return "This command can only be used inside a Discord server."
    if not user_can_manage_bot(interaction):
        return "You need Manage Server or Administrator permission to use this."
    return None



@tree.command(
    name="setup",
    description="Set subreddits, interval, media, and channel on one screen.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    subreddits="Subreddits to post, comma-separated (for example: bondage, aww)",
    interval="How often the bot should check this channel",
    media="What the bot should post in this channel",
    channel="Channel to set up (defaults to the current channel)",
    timerange="Which Reddit feed to use for this channel",
)
@app_commands.choices(
    interval=[
        app_commands.Choice(name="1 minute", value=1),
        app_commands.Choice(name="2 minutes", value=2),
        app_commands.Choice(name="5 minutes", value=5),
    ],
    media=[
        app_commands.Choice(name="videos only", value="videos"),
        app_commands.Choice(name="images only", value="images"),
        app_commands.Choice(name="both", value="both"),
    ],
    timerange=[
        app_commands.Choice(name="new", value="new"),
        app_commands.Choice(name="hot", value="hot"),
        app_commands.Choice(name="top today", value="top-day"),
        app_commands.Choice(name="top this week", value="top-week"),
    ],
)
async def setup_command(
    interaction: discord.Interaction,
    subreddits: str,
    interval: int,
    media: str | None = None,
    channel: discord.TextChannel | None = None,
    timerange: str | None = None,
):
    try:
        error = require_manage_server(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please use this command in a text channel or provide a text channel.",
                ephemeral=True,
            )
            return

        ok, message, summary = apply_setup_settings(
            discord_guild_id=str(interaction.guild_id),
            target_channel=target_channel,
            subreddits_text=subreddits,
            interval_minutes=interval,
            media_type=media,
            timerange=timerange,
        )
        if not ok or summary is None:
            await interaction.response.send_message(
                message or "I could not save that setup right now.",
                ephemeral=True,
            )
            return

        embed = build_setup_embed(summary, warning=message)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as error:
        print(f"/setup command failed: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong while saving the setup. The bot is still posting as before.",
                ephemeral=True,
            )


@setup_command.autocomplete("subreddits")
async def setup_subreddits_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    prefix, last = split_csv_autocomplete(current)
    query = last.strip()
    choices: list[app_commands.Choice[str]] = []

    results = []
    if query:
        try:
            results = await asyncio.to_thread(search_subreddits, query, 15)
        except Exception as error:
            print(f"Subreddit autocomplete failed: {error}")
            results = []

    for item in results:
        name = normalize_subreddit_name(item.get("name"))
        if not name or is_blocked_subreddit(name):
            continue
        value = f"{prefix}{name}" if prefix else name
        label = format_subreddit_choice(name, item.get("subscribers"))
        if prefix:
            label = f"{label}"
        if len(value) > 100:
            continue
        choices.append(app_commands.Choice(name=label[:100], value=value))
        if len(choices) >= 25:
            break

    if not choices:
        fallback = (current or query or "type a subreddit").strip()
        if not fallback:
            fallback = "type a subreddit"
        choices.append(
            app_commands.Choice(
                name=fallback[:100],
                value=fallback[:100],
            )
        )

    return choices[:25]


@tree.command(
    name="interval",
    description="Set how often the bot checks Reddit (1, 2, or 5 minutes).",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(minutes="How often the bot should check Reddit")
@app_commands.choices(
    minutes=[
        app_commands.Choice(name="1 minute", value=1),
        app_commands.Choice(name="2 minutes", value=2),
        app_commands.Choice(name="5 minutes", value=5),
    ]
)
async def interval_command(
    interaction: discord.Interaction,
    minutes: int,
):
    try:
        error = require_manage_server(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        parsed = parse_interval(minutes)
        if parsed is None:
            await interaction.response.send_message(
                "Please choose 1, 2, or 5 minutes.",
                ephemeral=True,
            )
            return

        saved = set_check_interval_minutes(parsed)
        applied = apply_effective_check_interval(fallback=parsed)

        if applied and saved:
            await interaction.response.send_message(
                f"Done. The bot will now check Reddit every **{parsed} minute(s)**.",
                ephemeral=True,
            )
            return

        if applied and not saved:
            await interaction.response.send_message(
                f"The bot will now check Reddit every **{parsed} minute(s)**, "
                "but I could not save this for the next restart. Try the command again later.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "I could not change the interval right now. The bot is still using the previous setting.",
            ephemeral=True,
        )
    except Exception as error:
        print(f"/interval command failed: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong while changing the interval. The bot is still posting as before.",
                ephemeral=True,
            )


@tree.command(
    name="media",
    description="Choose whether the bot posts videos, images, or both.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(media_type="What the bot should post")
@app_commands.choices(
    media_type=[
        app_commands.Choice(name="videos only", value="videos"),
        app_commands.Choice(name="images only", value="images"),
        app_commands.Choice(name="both", value="both"),
    ]
)
async def media_command(
    interaction: discord.Interaction,
    media_type: str,
):
    try:
        error = require_manage_server(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        kind = media_type.strip().lower() if isinstance(media_type, str) else ""
        if kind not in ALLOWED_MEDIA_TYPES:
            await interaction.response.send_message(
                "Please choose videos, images, or both.",
                ephemeral=True,
            )
            return

        saved = set_media_type(kind)
        labels = {
            "videos": "videos only",
            "images": "images only",
            "both": "both images and videos",
        }
        label = labels[kind]

        if saved:
            await interaction.response.send_message(
                f"Done. The bot will now post **{label}**.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "I could not save that media setting right now. The bot will keep using the previous setting.",
            ephemeral=True,
        )
    except Exception as error:
        print(f"/media command failed: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong while changing the media setting. The bot is still posting as before.",
                ephemeral=True,
            )


@tree.command(
    name="status",
    description="Show the current interval, media type, and mapped channels.",
)
@app_commands.default_permissions(manage_guild=True)
async def status_command(interaction: discord.Interaction):
    try:
        error = require_manage_server(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            interval = get_check_interval_minutes(default=CHECK_INTERVAL_MINUTES)
        except Exception:
            interval = CHECK_INTERVAL_MINUTES

        try:
            media_type = get_media_type()
        except Exception:
            media_type = "both"

        lines = [
            "Server defaults:",
            f"Check interval: **{interval} minute(s)**",
            f"Media type: **{media_type}**",
            "",
            "Mapped age-restricted channels:",
        ]

        try:
            jobs = collect_channel_jobs()
        except Exception as job_error:
            print(f"/status could not list channels: {job_error}")
            jobs = []

        settings_by_channel = {}
        try:
            for row in list_channel_settings(str(interaction.guild_id)):
                settings_by_channel[row["discord_channel_id"]] = row
        except Exception as settings_error:
            print(f"/status could not list channel settings: {settings_error}")

        seen_channels = set()
        mapped_lines = []
        for job in jobs:
            channel_id = job["discord_channel_id"]
            if channel_id in seen_channels:
                continue
            seen_channels.add(channel_id)
            names = " / ".join(f"r/{name}" for name in job["subreddit_candidates"])
            # include any extra subreddits later in the job list
            extra = []
            for other in jobs:
                if other["discord_channel_id"] != channel_id:
                    continue
                for name in other["subreddit_candidates"]:
                    label = f"r/{name}"
                    if label not in names and label not in extra:
                        extra.append(label)
            if extra:
                names = names + " / " + " / ".join(extra)

            channel_settings = settings_by_channel.get(channel_id) or {}
            job_interval = job.get("interval_minutes") or channel_settings.get("interval_minutes") or interval
            job_media = job.get("media_type") or channel_settings.get("media_type") or media_type
            job_feed = channel_settings.get("feed_modes") or job.get("feed_mode")
            detail = f"interval={job_interval} min, media={job_media}"
            if job_feed:
                detail += f", feed={job_feed}"
            mapped_lines.append(f"- <#{channel_id}> → {names} ({detail})")

        if mapped_lines:
            lines.extend(mapped_lines)
        else:
            lines.append("- None yet. Use /setup, or create an age-restricted channel named after the subreddit.")

        message = "\n".join(lines)
        if len(message) > 1800:
            message = message[:1800] + "\n...truncated"

        await interaction.response.send_message(message, ephemeral=True)
    except Exception as error:
        print(f"/status command failed: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong while reading the current settings.",
                ephemeral=True,
            )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is missing from .env")

    client.run(DISCORD_TOKEN)
