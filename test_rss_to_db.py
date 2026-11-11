import os

from db import init_db, has_been_posted, mark_as_posted
from reddit_fetcher import fetch_latest_posts


def main():
    if not os.getenv("DATABASE_URL"):
        print("Skipping RSS-to-database test: DATABASE_URL is not set.")
        return

    print("Starting Reddit RSS to database test...")
    init_db()
    posts = fetch_latest_posts(subreddit_name="python", feed_mode="new", limit=5)

    if not posts:
        print("No posts found.")
        return

    for post in posts:
        post_id = post["id"]
        title = post["title"]
        url = post["url"]
        subreddit = post["subreddit"]

        print("-" * 60)
        print("Title:", title)
        print("ID:", post_id)
        print("URL:", url)

        if has_been_posted(post_id):
            print("Result: Already exists in database. Skipping.")
        else:
            mark_as_posted(
                post_id=post_id,
                title=title,
                reddit_url=url,
                subreddit_name=subreddit,
                discord_channel_id="0",
            )
            print("Result: New post saved to database.")

    print("Test complete.")


if __name__ == "__main__":
    main()
