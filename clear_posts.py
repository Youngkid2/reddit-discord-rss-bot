from db import get_connection


def clear_posts_for_subreddit(subreddit_name: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM posted_posts
                WHERE subreddit_name = %s;
                """,
                (subreddit_name,),
            )

            deleted_count = cursor.rowcount

    print(f"Deleted {deleted_count} posts from r/{subreddit_name}.")


if __name__ == "__main__":
    clear_posts_for_subreddit("wallpapers")

