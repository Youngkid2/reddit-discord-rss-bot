from reddit_fetcher import fetch_latest_posts


def main():
    posts = fetch_latest_posts(
        subreddit_name="wallpapers",
        feed_mode="hot",
        limit=10,
    )

    for post in posts:
        print("-" * 90)
        print("Title:", post["title"])
        print("URL:", post["url"])
        print("Images found:", len(post["image_urls"]))
        print("Videos found:", len(post["video_urls"]))
        print("Has media candidate:", post.get("has_media_candidate"))
        print("Media hint:", post.get("media_hint"))

        for image_url in post["image_urls"]:
            print("  IMG:", image_url)

        for video_url in post["video_urls"]:
            print("  VID:", video_url)


if __name__ == "__main__":
    main()
