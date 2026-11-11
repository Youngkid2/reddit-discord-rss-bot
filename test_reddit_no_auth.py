import os

import requests
from dotenv import load_dotenv


def main():
    load_dotenv()

    subreddit_name = os.getenv("SUBREDDIT_NAME", "python")
    url = f"https://www.reddit.com/r/{subreddit_name}/new.json?limit=5"
    headers = {
        "User-Agent": "RedditDiscordBot/1.0 (RSS client; +https://github.com/bytedelay/reddit-discord-bot)"
    }

    response = requests.get(url, headers=headers, timeout=10)
    print("Status code:", response.status_code)
    response.raise_for_status()
    data = response.json()

    print(f"Latest posts from r/{subreddit_name}:")
    for item in data["data"]["children"]:
        post = item["data"]
        print("-" * 50)
        print("Title:", post["title"])
        print("ID:", post["id"])
        print("URL:", f"https://www.reddit.com{post['permalink']}")


if __name__ == "__main__":
    main()
