from db import get_connection


def remove_subreddit_config(subreddit_name: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM subreddit_configs
                WHERE subreddit_name = %s;
                """,
                (subreddit_name,),
            )

            deleted_count = cursor.rowcount

    print(f"Deleted {deleted_count} config(s) for r/{subreddit_name}.")


if __name__ == "__main__":
    remove_subreddit_config("wallpapers")
    remove_subreddit_config("EarthPorn")