from db import get_connection, init_db


def main():
    init_db()

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE posted_posts
                ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'posted';
                """
            )

            cursor.execute(
                """
                ALTER TABLE posted_posts
                ADD COLUMN IF NOT EXISTS skip_reason TEXT;
                """
            )

            cursor.execute(
                """
                UPDATE posted_posts
                SET status = 'posted'
                WHERE status IS NULL;
                """
            )

    print("Migration complete: added status and skip_reason to posted_posts.")


if __name__ == "__main__":
    main()