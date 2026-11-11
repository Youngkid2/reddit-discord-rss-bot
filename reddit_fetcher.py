import html
import time
from urllib.parse import urlparse
from typing import Dict, List

import feedparser
import requests
from bs4 import BeautifulSoup


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")


IMAGE_HOSTS = (
    "i.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "i.imgur.com",
    "imgur.com",
    "redditmedia.com",
)

VIDEO_HOSTS = (
    "v.redd.it",
    "redgifs.com",
    "gfycat.com",
)


def clean_url(url: str) -> str:
    return html.unescape(url).replace("&amp;", "&")


def url_without_query(url: str) -> str:
    return url.split("?")[0].lower()


def get_hostname(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_direct_image_url(url: str) -> bool:
    cleaned = clean_url(url)
    lower_url = cleaned.lower()
    host = get_hostname(cleaned)

    if url_without_query(cleaned).endswith(IMAGE_EXTENSIONS):
        return True

    if any(image_host in host for image_host in IMAGE_HOSTS):
        return True

    if "format=jpg" in lower_url or "format=png" in lower_url or "format=webp" in lower_url:
        return True

    return False


def is_video_url(url: str) -> bool:
    cleaned = clean_url(url)
    host = get_hostname(cleaned)

    if url_without_query(cleaned).endswith(VIDEO_EXTENSIONS):
        return True

    if any(video_host in host for video_host in VIDEO_HOSTS):
        return True

    return False


def dedupe_urls(urls: List[str]) -> List[str]:
    seen = set()
    unique_urls = []

    for url in urls:
        if not url:
            continue

        cleaned = clean_url(url)

        if cleaned not in seen:
            seen.add(cleaned)
            unique_urls.append(cleaned)

    return unique_urls


def get_html_blocks(entry) -> List[str]:
    html_blocks = []

    summary = entry.get("summary", "")
    description = entry.get("description", "")

    if summary:
        html_blocks.append(summary)

    if description:
        html_blocks.append(description)

    for content_item in entry.get("content", []):
        value = content_item.get("value", "")
        if value:
            html_blocks.append(value)

    return html_blocks


def extract_media_urls(entry) -> Dict[str, List[str]]:
    image_urls = []
    video_urls = []

    # 1. RSS media thumbnails
    if hasattr(entry, "media_thumbnail"):
        for thumbnail in entry.media_thumbnail:
            url = thumbnail.get("url")
            if url:
                image_urls.append(url)

    # 2. RSS media content
    if hasattr(entry, "media_content"):
        for media_item in entry.media_content:
            url = media_item.get("url")
            if not url:
                continue

            if is_video_url(url):
                video_urls.append(url)
            elif is_direct_image_url(url):
                image_urls.append(url)

    # 3. Parse summary/content HTML for images and media links
    for html_block in get_html_blocks(entry):
        soup = BeautifulSoup(html_block, "html.parser")

        for img in soup.find_all("img"):
            src = img.get("src")
            if src and is_direct_image_url(src):
                image_urls.append(src)

        for source in soup.find_all("source"):
            src = source.get("src")
            if src and is_video_url(src):
                video_urls.append(src)

        for video in soup.find_all("video"):
            src = video.get("src")
            if src and is_video_url(src):
                video_urls.append(src)

        for link in soup.find_all("a"):
            href = link.get("href")
            if not href:
                continue

            if is_direct_image_url(href):
                image_urls.append(href)

            elif is_video_url(href):
                video_urls.append(href)

    # 4. Feed links/enclosures
    for link in entry.get("links", []):
        href = link.get("href", "")

        if is_direct_image_url(href):
            image_urls.append(href)

        elif is_video_url(href):
            video_urls.append(href)

    return {
        "image_urls": dedupe_urls(image_urls),
        "video_urls": dedupe_urls(video_urls),
    }


def build_feed_url(subreddit_name: str, feed_mode: str) -> str:
    feed_mode = feed_mode.lower().strip()

    if feed_mode == "old":
        return f"https://www.reddit.com/r/{subreddit_name}/new/.rss"

    if feed_mode in ["new", "hot", "top", "rising"]:
        return f"https://www.reddit.com/r/{subreddit_name}/{feed_mode}/.rss"

    raise ValueError(f"Unsupported feed mode: {feed_mode}")


def fetch_rss_with_retry(
    feed_url: str,
    subreddit_name: str,
    feed_mode: str,
    max_retries: int = 2,
):
    headers = {
        "User-Agent": "RedditDiscordBot/1.0 (RSS client; +https://github.com/bytedelay/reddit-discord-bot)"
    }

    wait_seconds = 10

    for attempt in range(1, max_retries + 1):
        response = requests.get(feed_url, headers=headers, timeout=10)

        print(
            f"Reddit RSS status for r/{subreddit_name} [{feed_mode}]: "
            f"{response.status_code} "
            f"(attempt {attempt}/{max_retries})"
        )

        if response.status_code == 200:
            return response.text

        if response.status_code == 404:
            print(
                f"Subreddit r/{subreddit_name} [{feed_mode}] not found (404)."
            )
            return None

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            if retry_after and retry_after.isdigit():
                wait_seconds = int(retry_after)

            print(
                f"Rate limited for r/{subreddit_name} [{feed_mode}]. "
                f"Waiting {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue

        response.raise_for_status()

    print(f"Failed to fetch r/{subreddit_name} [{feed_mode}] after retries.")
    return None

def detect_media_candidate(entry) -> tuple[bool, str]:
    """
    RSS may not expose full carousel/gallery data.
    This function detects posts that likely contain media even when
    we cannot extract direct image/video URLs.
    """

    entry_link = entry.get("link", "").lower()
    title = entry.get("title", "").lower()
    summary = entry.get("summary", "").lower()
    description = entry.get("description", "").lower()

    combined_text = f"{entry_link} {title} {summary} {description}"

    if "/gallery/" in combined_text:
        return True, "possible_gallery"

    if "gallery" in combined_text or "carousel" in combined_text:
        return True, "possible_gallery"

    if "preview.redd.it" in combined_text:
        return True, "reddit_preview"

    if "i.redd.it" in combined_text:
        return True, "reddit_image"

    if "v.redd.it" in combined_text:
        return True, "reddit_video"

    if "<img" in combined_text:
        return True, "rss_image_present"

    return False, "no_media_detected"

def fetch_latest_posts(
    subreddit_name: str,
    feed_mode: str = "new",
    limit: int = 25,
) -> List[Dict]:
    feed_mode = feed_mode.lower().strip()

    feed_url = build_feed_url(
        subreddit_name=subreddit_name,
        feed_mode=feed_mode,
    )

    rss_text = fetch_rss_with_retry(
        feed_url=feed_url,
        subreddit_name=subreddit_name,
        feed_mode=feed_mode,
        max_retries=2,
    )

    if rss_text is None:
        return []

    feed = feedparser.parse(rss_text)

    entries = list(feed.entries[:limit])

    if feed_mode == "old":
        entries.reverse()

    posts = []

    for entry in entries:
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
                "subreddit": subreddit_name,
                "feed_mode": feed_mode,
            }
        )

    return posts