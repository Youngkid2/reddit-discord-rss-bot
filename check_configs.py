from db import get_active_subreddit_configs


configs = get_active_subreddit_configs()

if not configs:
    print("No active subreddit configs found.")
else:
    print("Active subreddit configs:")

    for subreddit_name, discord_channel_id in configs:
        print(f"r/{subreddit_name} -> Discord channel {discord_channel_id}")