from reddit_fetcher import fetch_latest_posts


def main():
    posts = fetch_latest_posts(
        subreddit_name="python",
        feed_mode="new",
        limit=5,
    )

    if not posts:
        print("No posts found.")
        return

    for post in posts:
        print("-" * 80)
        print("Title:", post["title"])
        print("URL:", post["url"])
        print("Images:", post.get("image_urls"))


if __name__ == "__main__":
    main()
