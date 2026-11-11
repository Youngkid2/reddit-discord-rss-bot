import os

from dotenv import load_dotenv

from db import init_db, add_subreddit_config


load_dotenv(override=True)


def main():
    init_db()

    discord_channel_id = os.getenv("DISCORD_CHANNEL_ID")

    configs_to_add = [
    "PORNism",
    "NSFWMemes ",
    ]

    if not discord_channel_id:
        raise ValueError("DISCORD_CHANNEL_ID is missing from .env")

    for subreddit_name in configs_to_add:
        add_subreddit_config(
            subreddit_name=subreddit_name,
            discord_channel_id=discord_channel_id,
        )


if __name__ == "__main__":
    main()