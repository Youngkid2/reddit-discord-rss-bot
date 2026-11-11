import os 

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is missing. Check your .env file.")

    return psycopg.connect(DATABASE_URL)

def init_db() -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            # Table 1: stores Reddit posts that were already sent to Discord
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_posts (
                    reddit_post_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    reddit_url TEXT NOT NULL,
                    subreddit_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            # Table 2: stores which subreddit should post to which Discord channel
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS subreddit_configs (
                id SERIAL PRIMARY KEY,
                subreddit_name TEXT NOT NULL,
                discord_channel_id TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (subreddit_name, discord_channel_id)
                );
                """
            )

            cursor.execute(
                """
                ALTER TABLE subreddit_configs
                ADD COLUMN IF NOT EXISTS include_carousels
                BOOLEAN NOT NULL DEFAULT FALSE;
                """
            )

    print("Database initialized successfully.")


def has_been_posted(post_id: str) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM posted_posts
                WHERE reddit_post_id = %s;
                """,
                (post_id,),
            )

            return cursor.fetchone() is not None
        
def mark_as_posted(
    post_id: str,
    title: str,
    reddit_url: str,
    subreddit_name: str,
    discord_channel_id: str,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO posted_posts (
                    reddit_post_id,
                    title,
                    reddit_url,
                    subreddit_name,
                    discord_channel_id,
                    status,
                    skip_reason
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reddit_post_id, discord_channel_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    reddit_url = EXCLUDED.reddit_url,
                    subreddit_name = EXCLUDED.subreddit_name,
                    status = EXCLUDED.status,
                    skip_reason = EXCLUDED.skip_reason;
                """,
                (
                    post_id,
                    title,
                    reddit_url,
                    subreddit_name,
                    discord_channel_id,
                    "posted",
                    None,
                ),
            )

    print(f"Marked post as posted: {post_id} in channel {discord_channel_id}")


def add_subreddit_config(subreddit_name: str, discord_channel_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO subreddit_configs (
                    subreddit_name,
                    discord_channel_id
                )
                VALUES (%s, %s)
                ON CONFLICT (subreddit_name, discord_channel_id) DO NOTHING;
                """,
                (subreddit_name, discord_channel_id),
            )

    print(f"Added config: r/{subreddit_name} -> channel {discord_channel_id}")

def get_active_subreddit_configs():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    subreddit_name,
                    discord_channel_id,
                    COALESCE(feed_modes, 'old,hot,new') AS feed_modes,
                    COALESCE(post_limit, 25) AS post_limit,
                    discord_guild_id,
                    COALESCE(include_carousels, FALSE) AS include_carousels
                FROM subreddit_configs
                WHERE is_active = TRUE
                ORDER BY id ASC;
                """
            )

            return cursor.fetchall()
        
def has_been_processed(post_id: str, discord_channel_id: str) -> bool:
    """
    Returns True if this Reddit post was already processed for this specific
    Discord channel.
    """
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM posted_posts
                WHERE reddit_post_id = %s
                AND discord_channel_id = %s;
                """,
                (post_id, discord_channel_id),
            )

            return cursor.fetchone() is not None


def mark_as_skipped(
    post_id: str,
    title: str,
    reddit_url: str,
    subreddit_name: str,
    discord_channel_id: str,
    reason: str,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO posted_posts (
                    reddit_post_id,
                    title,
                    reddit_url,
                    subreddit_name,
                    discord_channel_id,
                    status,
                    skip_reason
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reddit_post_id, discord_channel_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    reddit_url = EXCLUDED.reddit_url,
                    subreddit_name = EXCLUDED.subreddit_name,
                    status = EXCLUDED.status,
                    skip_reason = EXCLUDED.skip_reason;
                """,
                (
                    post_id,
                    title,
                    reddit_url,
                    subreddit_name,
                    discord_channel_id,
                    "skipped",
                    reason,
                ),
            )

    print(
        f"Marked post as skipped: {post_id} "
        f"in channel {discord_channel_id} | Reason: {reason}"
    )

def list_subreddit_configs(discord_guild_id: str | None = None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    SELECT
                        id,
                        subreddit_name,
                        discord_channel_id,
                        is_active,
                        COALESCE(feed_modes, 'old,hot,new') AS feed_modes,
                        COALESCE(post_limit, 25) AS post_limit,
                        COALESCE(include_carousels, FALSE) AS include_carousels,
                        created_at
                    FROM subreddit_configs
                    WHERE discord_guild_id = %s
                    ORDER BY id ASC;
                    """,
                    (discord_guild_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        id,
                        subreddit_name,
                        discord_channel_id,
                        is_active,
                        COALESCE(feed_modes, 'old,hot,new') AS feed_modes,
                        COALESCE(post_limit, 25) AS post_limit,
                        COALESCE(include_carousels, FALSE) AS include_carousels,
                        created_at
                    FROM subreddit_configs
                    ORDER BY id ASC;
                    """
                )
            return cursor.fetchall()
        
def upsert_subreddit_config(
    subreddit_name: str,
    discord_channel_id: str,
    feed_modes: str = "old,hot,new",
    post_limit: int = 25,
    discord_guild_id: str | None = None,
):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO subreddit_configs (
                    subreddit_name,
                    discord_channel_id,
                    discord_guild_id,
                    is_active,
                    feed_modes,
                    post_limit
                )
                VALUES (%s, %s, %s, TRUE, %s, %s)
                ON CONFLICT (subreddit_name, discord_channel_id)
                DO UPDATE SET
                    discord_guild_id = EXCLUDED.discord_guild_id,
                    is_active = TRUE,
                    feed_modes = EXCLUDED.feed_modes,
                    post_limit = EXCLUDED.post_limit;
                """,
                (
                    subreddit_name,
                    discord_channel_id,
                    discord_guild_id,
                    feed_modes,
                    post_limit,
                ),
            )

def remove_subreddit_config(
    subreddit_name: str,
    discord_guild_id: str | None = None,
):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    DELETE FROM subreddit_configs
                    WHERE LOWER(subreddit_name) = LOWER(%s)
                    AND discord_guild_id = %s;
                    """,
                    (subreddit_name, discord_guild_id),
                )
            else:
                cursor.execute(
                    """
                    DELETE FROM subreddit_configs
                    WHERE LOWER(subreddit_name) = LOWER(%s);
                    """,
                    (subreddit_name,),
                )

            return cursor.rowcount


def set_subreddit_active(
    subreddit_name: str,
    is_active: bool,
    discord_guild_id: str | None = None,
):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    UPDATE subreddit_configs
                    SET is_active = %s
                    WHERE LOWER(subreddit_name) = LOWER(%s)
                    AND discord_guild_id = %s;
                    """,
                    (is_active, subreddit_name, discord_guild_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE subreddit_configs
                    SET is_active = %s
                    WHERE LOWER(subreddit_name) = LOWER(%s);
                    """,
                    (is_active, subreddit_name),
                )

            return cursor.rowcount


def clear_processed_posts_for_subreddit(
    subreddit_name: str,
    discord_guild_id: str | None = None,
):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    DELETE FROM posted_posts
                    WHERE LOWER(subreddit_name) = LOWER(%s)
                    AND discord_channel_id IN (
                        SELECT discord_channel_id
                        FROM subreddit_configs
                        WHERE discord_guild_id = %s
                    );
                    """,
                    (subreddit_name, discord_guild_id),
                )
            else:
                cursor.execute(
                    """
                    DELETE FROM posted_posts
                    WHERE LOWER(subreddit_name) = LOWER(%s);
                    """,
                    (subreddit_name,),
                )

            return cursor.rowcount
        
def set_subreddit_carousels(
    subreddit_name: str,
    include_carousels: bool,
    discord_guild_id: str | None = None,
) -> int:
    """
    Enable or disable carousel/gallery posts for a subreddit config.
    """
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    UPDATE subreddit_configs
                    SET include_carousels = %s
                    WHERE LOWER(subreddit_name) = LOWER(%s)
                    AND discord_guild_id = %s;
                    """,
                    (
                        include_carousels,
                        subreddit_name,
                        discord_guild_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE subreddit_configs
                    SET include_carousels = %s
                    WHERE LOWER(subreddit_name) = LOWER(%s);
                    """,
                    (
                        include_carousels,
                        subreddit_name,
                    ),
                )

            return cursor.rowcount