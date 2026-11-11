import os

import feedparser
import requests
from dotenv import load_dotenv


def fetch_feed(subreddit_name: str):
    feed_urls = [
        f"https://www.reddit.com/r/{subreddit_name}/new/.rss",
        f"https://old.reddit.com/r/{subreddit_name}/new/.rss",
    ]
    headers = {
        "User-Agent": "RedditDiscordBot/1.0 (RSS client; +https://github.com/bytedelay/reddit-discord-bot)"
    }
    last_error = None

    for url in feed_urls:
        print(f"Trying feed URL: {url}")
        try:
            response = requests.get(url, headers=headers, timeout=10)
            print("Status code:", response.status_code)
            if response.status_code == 200:
                return feedparser.parse(response.text)
            last_error = f"Failed with status {response.status_code}"
        except requests.RequestException as error:
            last_error = str(error)

    raise RuntimeError(f"Could not fetch Reddit RSS feed. Last error: {last_error}")


def main():
    load_dotenv()
    subreddit_name = os.getenv("SUBREDDIT_NAME", "wallpapers")
    feed = fetch_feed(subreddit_name)
    print(f"\nLatest posts from r/{subreddit_name}:")
    if not feed.entries:
        print("No entries found.")
        return
    for entry in feed.entries[:5]:
        print("-" * 60)
        print("Title:", entry.title)
        print("ID:", entry.id)
        print("URL:", entry.link)


if __name__ == "__main__":
    main()
