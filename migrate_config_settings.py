from db import get_connection, init_db


def main():
    init_db()

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE subreddit_configs
                ADD COLUMN IF NOT EXISTS feed_modes TEXT DEFAULT 'old,hot,new';
                """
            )

            cursor.execute(
                """
                ALTER TABLE subreddit_configs
                ADD COLUMN IF NOT EXISTS post_limit INTEGER DEFAULT 25;
                """
            )

    print("Migration complete: added feed_modes and post_limit to subreddit_configs.")


if __name__ == "__main__":
    main()