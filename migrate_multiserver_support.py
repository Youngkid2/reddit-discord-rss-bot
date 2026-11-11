import os

from dotenv import load_dotenv

from db import get_connection, init_db


load_dotenv(override=True)


def main():
    init_db()

    default_guild_id = os.getenv("DISCORD_GUILD_ID", "legacy")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            # Add channel-specific post history.
            cursor.execute(
                """
                ALTER TABLE posted_posts
                ADD COLUMN IF NOT EXISTS discord_channel_id TEXT;
                """
            )

            cursor.execute(
                """
                UPDATE posted_posts
                SET discord_channel_id = 'legacy'
                WHERE discord_channel_id IS NULL;
                """
            )

            cursor.execute(
                """
                ALTER TABLE posted_posts
                ALTER COLUMN discord_channel_id SET NOT NULL;
                """
            )

            # Remove old single-column primary key if it exists.
            cursor.execute(
                """
                ALTER TABLE posted_posts
                DROP CONSTRAINT IF EXISTS posted_posts_pkey;
                """
            )

            # New duplicate rule:
            # same Reddit post can appear in different Discord channels,
            # but not twice in the same Discord channel.
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS posted_posts_post_channel_uq
                ON posted_posts (reddit_post_id, discord_channel_id);
                """
            )

            # Add guild/server ownership to subreddit configs.
            cursor.execute(
                """
                ALTER TABLE subreddit_configs
                ADD COLUMN IF NOT EXISTS discord_guild_id TEXT;
                """
            )

            cursor.execute(
                """
                UPDATE subreddit_configs
                SET discord_guild_id = %s
                WHERE discord_guild_id IS NULL;
                """,
                (default_guild_id,),
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS subreddit_configs_guild_idx
                ON subreddit_configs (discord_guild_id);
                """
            )

    print("Migration complete: multi-server support added.")


if __name__ == "__main__":
    main()