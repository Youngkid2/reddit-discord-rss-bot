import html
import re
import json
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


FEED_MODE_ALIASES = {
    "old": "old",
    "new": "new",
    "hot": "hot",
    "rising": "rising",
    "top": "top",
    "top-day": "top-day",
    "top_day": "top-day",
    "topday": "top-day",
    "top-week": "top-week",
    "top_week": "top-week",
    "topweek": "top-week",
    "top-month": "top-month",
    "top_month": "top-month",
    "top-year": "top-year",
    "top_year": "top-year",
    "top-all": "top",
}

TIMERANGE_CHOICES = ("new", "hot", "top-day", "top-week")


def normalize_feed_mode(feed_mode: str | None) -> str | None:
    if not feed_mode:
        return None
    key = str(feed_mode).strip().lower().replace(" ", "-")
    return FEED_MODE_ALIASES.get(key)


def build_feed_url(subreddit_name: str, feed_mode: str) -> str:
    normalized = normalize_feed_mode(feed_mode)
    if not normalized:
        raise ValueError(f"Unsupported feed mode: {feed_mode}")

    if normalized == "old":
        return f"https://www.reddit.com/r/{subreddit_name}/new/.rss"

    if normalized.startswith("top-"):
        period = normalized.split("-", 1)[1]
        return f"https://www.reddit.com/r/{subreddit_name}/top/.rss?t={period}"

    return f"https://www.reddit.com/r/{subreddit_name}/{normalized}/.rss"


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
    feed_mode = normalize_feed_mode(feed_mode) or feed_mode.lower().strip()

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


USER_AGENT = "RedditDiscordBot/1.0 (RSS client; +https://github.com/bytedelay/reddit-discord-bot)"

_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_CACHE_SECONDS = 60


def _reddit_headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def format_media_key(url: str) -> str | None:
    """Stable id for a media URL, ignoring host/query differences when possible."""
    if not url:
        return None
    cleaned = clean_url(url)
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    parts = [part for part in path.split("/") if part]

    if host in {"redgifs.com", "www.redgifs.com"} and len(parts) >= 2:
        return f"redgifs:{parts[1].lower()}"
    if host.endswith("redgifs.com") and parts:
        return f"redgifs:{parts[-1].split('.')[0].lower()}"

    if host in {"v.redd.it", "i.redd.it", "preview.redd.it", "external-preview.redd.it"}:
        if not parts:
            return None
        file_id = parts[0]
        file_id = file_id.split("?")[0]
        file_id = file_id.rsplit(".", 1)[0].lower()
        kind = "video" if host == "v.redd.it" else "image"
        return f"{kind}:{file_id}"

    filename = parts[-1] if parts else ""
    file_id = filename.split("?")[0].rsplit(".", 1)[0].lower()
    if file_id:
        return f"{host}:{file_id}"
    if cleaned:
        return url_without_query(cleaned)
    return None


def preferred_image_urls(image_urls: List[str]) -> List[str]:
    """
    Collapse preview+full copies of the same Reddit image into one URL.

    RSS often lists both preview.redd.it (thumbnail) and i.redd.it (full
    file) for the same picture. Prefer the full i.redd.it file.
    """
    ranked: dict[str, tuple[int, str]] = {}
    leftovers: list[str] = []

    def rank(url: str) -> int:
        host = get_hostname(url)
        if "external-preview" in host or "external-preview" in url:
            return 4
        if "i.redd.it" in host:
            return 0
        if "preview.redd.it" in host:
            return 1
        if "imgur.com" in host or "redditmedia.com" in host:
            return 2
        return 3

    for url in dedupe_urls(image_urls or []):
        key = format_media_key(url)
        if not key:
            leftovers.append(url)
            continue
        current = ranked.get(key)
        this_rank = rank(url)
        if current is None or this_rank < current[0]:
            ranked[key] = (this_rank, url)

    # Keep original-ish order: first time we see a key wins its position.
    ordered: list[str] = []
    seen = set()
    for url in dedupe_urls(image_urls or []):
        key = format_media_key(url)
        if key and key not in seen and key in ranked:
            seen.add(key)
            ordered.append(ranked[key][1])
    for url in leftovers:
        if url not in ordered:
            ordered.append(url)
    return ordered


def preferred_video_urls(video_urls: List[str]) -> List[str]:
    ordered: list[str] = []
    seen = set()
    for url in dedupe_urls(video_urls or []):
        key = format_media_key(url) or url_without_query(url)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(url)
    return ordered


def media_keys_for_post(post: dict) -> List[str]:
    """Return the dedupe keys (post id, parent id, media URLs) for a post."""
    keys: list[str] = []
    seen = set()

    def add(key: str | None) -> None:
        if not key:
            return
        value = str(key).strip()
        if not value or value in seen:
            return
        seen.add(value)
        keys.append(value)

    add(_reddit_thing_id(post.get("id")))
    add(_reddit_thing_id(post.get("url")))
    add(_reddit_thing_id(post.get("crosspost_parent")))
    add(_reddit_thing_id(post.get("crosspost_parent_list")))

    for url in list(post.get("image_urls") or []) + list(post.get("video_urls") or []):
        add(format_media_key(url))

    return keys


def _reddit_thing_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            found = _reddit_thing_id(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        return _reddit_thing_id(
            value.get("id")
            or value.get("name")
            or value.get("permalink")
            or value.get("url")
        )

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"/comments/([a-z0-9]+)/", text, re.I)
    if match:
        return f"post:{match.group(1).lower()}"

    match = re.search(r"\bt3_([a-z0-9]+)\b", text, re.I)
    if match:
        return f"post:{match.group(1).lower()}"

    if re.fullmatch(r"[a-z0-9]{5,10}", text, re.I):
        return f"post:{text.lower()}"

    return None


def _parse_subreddit_search_payload(payload) -> list[dict]:
    results: list[dict] = []
    children = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            children = payload["data"].get("children") or []
        elif isinstance(payload.get("subreddits"), list):
            children = payload["subreddits"]
        elif isinstance(payload.get("names"), list):
            children = [{"display_name": name} for name in payload["names"]]

    for child in children:
        if not isinstance(child, dict):
            continue
        data = child.get("data") if isinstance(child.get("data"), dict) else child
        kind = child.get("kind")
        if kind and kind not in {"t5", "subreddit"}:
            # Skip user profiles and other result types.
            if not data.get("display_name") and not data.get("name"):
                continue
            if kind != "t5" and "subscribers" not in data and "numSubscribers" not in data:
                continue
        name = (
            data.get("display_name")
            or data.get("name")
            or str(data.get("display_name_prefixed") or "").lstrip("r/")
        )
        if not name or not isinstance(name, str):
            continue
        name = name.strip()
        if name.lower().startswith("u/") or name.startswith("u_"):
            continue
        subscribers = (
            data.get("subscribers")
            if data.get("subscribers") is not None
            else data.get("numSubscribers", data.get("subscriber_count"))
        )
        results.append({"name": name, "subscribers": subscribers})
    return results


def search_subreddits(query: str, limit: int = 10) -> list[dict]:
    """
    Search public Reddit subreddit autocomplete/search JSON.

    Results are cached briefly. On network or API failure, returns [].
    """
    cleaned = (query or "").strip().lstrip("r/").strip()
    if not cleaned:
        return []

    cache_key = f"{cleaned.lower()}::{limit}"
    now = time.time()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return list(cached[1])

    endpoints = [
        (
            "https://www.reddit.com/api/subreddit_autocomplete_v2.json",
            {
                "query": cleaned,
                "include_over_18": "true",
                "include_profiles": "false",
                "limit": str(max(1, min(int(limit), 25))),
            },
        ),
        (
            "https://www.reddit.com/subreddits/search.json",
            {
                "q": cleaned,
                "limit": str(max(1, min(int(limit), 25))),
                "include_over_18": "on",
                "raw_json": "1",
            },
        ),
    ]

    results: list[dict] = []
    for url, params in endpoints:
        try:
            response = requests.get(
                url,
                params=params,
                headers=_reddit_headers(),
                timeout=8,
            )
            if response.status_code != 200:
                continue
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "json" not in content_type and not response.text.lstrip().startswith(("{", "[")):
                continue
            payload = response.json()
            results = _parse_subreddit_search_payload(payload)
            if results:
                break
        except Exception as error:
            print(f"Subreddit search failed for {url}: {error}")
            continue

    _SEARCH_CACHE[cache_key] = (now + _SEARCH_CACHE_SECONDS, list(results))
    return results
