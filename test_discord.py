import os

import discord
from dotenv import load_dotenv


def main():
    load_dotenv(override=True)

    discord_token = os.getenv("DISCORD_TOKEN")
    channel_id_raw = os.getenv("DISCORD_CHANNEL_ID")

    if not discord_token or not channel_id_raw:
        print("Skipping Discord live test: DISCORD_TOKEN or DISCORD_CHANNEL_ID is not set.")
        return

    discord_channel_id = int(channel_id_raw)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}")

        channel = client.get_channel(discord_channel_id)

        if channel is None:
            channel = await client.fetch_channel(discord_channel_id)

        await channel.send("DRBOT test message: Discord connection is working.")

        print("Test message sent.")
        await client.close()

    client.run(discord_token)


if __name__ == "__main__":
    main()
