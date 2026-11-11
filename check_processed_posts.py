from db import get_connection


def main():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    reddit_post_id,
                    title,
                    subreddit_name,
                    status,
                    skip_reason,
                    created_at
                FROM posted_posts
                ORDER BY created_at DESC
                LIMIT 30;
                """
            )

            rows = cursor.fetchall()

    if not rows:
        print("No processed posts found.")
        return

    for row in rows:
        post_id, title, subreddit, status, skip_reason, created_at = row

        print("-" * 90)
        print("ID:", post_id)
        print("Subreddit:", subreddit)
        print("Status:", status)
        print("Skip reason:", skip_reason)
        print("Title:", title)
        print("Created:", created_at)


if __name__ == "__main__":
    main()