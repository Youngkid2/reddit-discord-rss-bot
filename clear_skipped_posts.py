from db import get_connection


def main():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM posted_posts
                WHERE status = 'skipped'
                AND skip_reason = 'no_media_found';
                """
            )

            deleted_count = cursor.rowcount

    print(f"Deleted {deleted_count} wrongly skipped no-media posts.")


if __name__ == "__main__":
    main()