from db import get_connection


def main():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    reddit_post_id,
                    title,
                    reddit_url,
                    subreddit_name,
                    created_at
                FROM posted_posts
                ORDER BY created_at DESC;
                """
            )

            rows = cursor.fetchall()

    if not rows:
        print("No posts found in database.")
        return

    print(f"Found {len(rows)} posts in database.")
    print("-" * 80)

    for row in rows:
        reddit_post_id, title, reddit_url, subreddit_name, created_at = row

        print("Post ID:", reddit_post_id)
        print("Title:", title)
        print("URL:", reddit_url)
        print("Subreddit:", subreddit_name)
        print("Created at:", created_at)
        print("-" * 80)


if __name__ == "__main__":
    main()