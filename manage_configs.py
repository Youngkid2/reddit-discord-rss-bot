import argparse

from db import get_connection, init_db


def list_configs():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    subreddit_name,
                    discord_channel_id,
                    is_active,
                    COALESCE(feed_modes, 'old,hot,new') AS feed_modes,
                    COALESCE(post_limit, 25) AS post_limit,
                    created_at
                FROM subreddit_configs
                ORDER BY id ASC;
                """
            )
            rows = cursor.fetchall()

    if not rows:
        print("No subreddit configs found.")
        return

    print("Subreddit configs:")
    print("-" * 90)

    for row in rows:
        (
            config_id,
            subreddit_name,
            channel_id,
            is_active,
            feed_modes,
            post_limit,
            created_at,
        ) = row

        status = "ACTIVE" if is_active else "INACTIVE"

        print(f"ID: {config_id}")
        print(f"Subreddit: r/{subreddit_name}")
        print(f"Discord Channel ID: {channel_id}")
        print(f"Status: {status}")
        print(f"Feed modes: {feed_modes}")
        print(f"Post limit: {post_limit}")
        print(f"Created at: {created_at}")
        print("-" * 90)

def add_config(
    subreddit_name: str,
    discord_channel_id: str,
    feed_modes: str = "old,hot,new",
    post_limit: int = 25,
):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO subreddit_configs (
                    subreddit_name,
                    discord_channel_id,
                    is_active,
                    feed_modes,
                    post_limit
                )
                VALUES (%s, %s, TRUE, %s, %s)
                ON CONFLICT (subreddit_name, discord_channel_id)
                DO UPDATE SET
                    is_active = TRUE,
                    feed_modes = EXCLUDED.feed_modes,
                    post_limit = EXCLUDED.post_limit;
                """,
                (subreddit_name, discord_channel_id, feed_modes, post_limit),
            )

    print(
        f"Added/activated r/{subreddit_name} -> channel {discord_channel_id} "
        f"with modes={feed_modes}, post_limit={post_limit}"
    )

def remove_config(subreddit_name: str):
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


def deactivate_config(subreddit_name: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE subreddit_configs
                SET is_active = FALSE
                WHERE subreddit_name = %s;
                """,
                (subreddit_name,),
            )

            updated_count = cursor.rowcount

    print(f"Deactivated {updated_count} config(s) for r/{subreddit_name}.")


def activate_config(subreddit_name: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE subreddit_configs
                SET is_active = TRUE
                WHERE subreddit_name = %s;
                """,
                (subreddit_name,),
            )

            updated_count = cursor.rowcount

    print(f"Activated {updated_count} config(s) for r/{subreddit_name}.")


def clear_posts(subreddit_name: str):
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

    print(f"Deleted {deleted_count} posted post(s) for r/{subreddit_name}.")


def main():
    init_db()

    parser = argparse.ArgumentParser(
        description="Manage subreddit configs for DRBOT."
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all subreddit configs")

    add_parser = subparsers.add_parser("add", help="Add a subreddit config")
    add_parser.add_argument("subreddit_name")
    add_parser.add_argument("discord_channel_id")
    add_parser.add_argument(
        "--modes",
        default="old,hot,new",
        help="Comma-separated feed modes, e.g. old,hot,new or hot,new",
    )
    add_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Number of candidate posts to check per cycle",
    )

    remove_parser = subparsers.add_parser("remove", help="Remove a subreddit config")
    remove_parser.add_argument("subreddit_name")

    deactivate_parser = subparsers.add_parser(
        "deactivate",
        help="Deactivate a subreddit config without deleting it",
    )
    deactivate_parser.add_argument("subreddit_name")

    activate_parser = subparsers.add_parser(
        "activate",
        help="Activate a subreddit config",
    )
    activate_parser.add_argument("subreddit_name")

    clear_parser = subparsers.add_parser(
        "clear-posts",
        help="Clear already-posted records for a subreddit",
    )
    clear_parser.add_argument("subreddit_name")

    args = parser.parse_args()

    if args.command == "list":
        list_configs()

    elif args.command == "add":
        add_config(
            subreddit_name=args.subreddit_name,
            discord_channel_id=args.discord_channel_id,
            feed_modes=args.modes,
            post_limit=args.limit,
            )

    elif args.command == "remove":
        remove_config(args.subreddit_name)

    elif args.command == "deactivate":
        deactivate_config(args.subreddit_name)

    elif args.command == "activate":
        activate_config(args.subreddit_name)

    elif args.command == "clear-posts":
        clear_posts(args.subreddit_name)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()