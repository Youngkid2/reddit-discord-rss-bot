# Reddit → Discord bot

Posts new Reddit media into Discord. Each **age-restricted** text channel is mapped to the subreddit of the same name via Reddit's public RSS feed (`https://www.reddit.com/r/<name>/new/.rss`). No channel list is required.

## How mapping works

- Channel `#xyz` receives posts from `r/xyz`.
- Matching is case-insensitive. If the channel name has hyphens (Discord-style), the bot tries the name as-is and then with `-` replaced by `_` (subreddit-style).
- Optional ignore list: set `IGNORE_CHANNELS` (defaults to `general,rules,announcements`).
- Optional overrides: set `CHANNEL_SUBREDDIT_MAP` like `cute-pics:aww,my-channel:Some_Sub`.
- Existing slash-command configs (`/drbot_add`) still work and override the auto name for that channel.

## Age-restricted channels only

The bot posts **only** into channels marked age-restricted (`channel.nsfw` / `is_nsfw()`). Any other channel is skipped and logged. This is enforced on the scheduled loop and on slash commands.

## Commands

Anyone with **Manage Server** or Administrator permission can use these in the Discord server:

- `/setup` — the main control panel. On one screen you can pick **subreddits** (comma-separated, with autocomplete and member counts), **interval** (1, 2, or 5 minutes), optional **media** (videos, images, or both), optional **channel** (defaults to the current channel), and optional **timerange** (new, hot, top today, or top this week). Settings are saved per channel. The bot still uses the server-wide `/interval` and `/media` values when a channel has no override. The target channel must be age-restricted.
- `/interval` — choose **1**, **2**, or **5** minutes as the server-wide default. This changes how often the bot checks Reddit, right away, and the choice is remembered after a restart.
- `/media` — choose **videos**, **images**, or **both** as the server-wide default. This filters what gets posted and is also remembered.
- `/status` — shows the server-wide interval and media type, plus each mapped channel's own settings.

The default interval is still `CHECK_INTERVAL_MINUTES` (5 minutes) until you change it. The default media type is both.

If the new commands do not appear after a restart, the bot needs the `applications.commands` scope. Re-invite the **same** bot with that scope, plus the existing permissions (View Channels, Send Messages, Embed Links, Attach Files, Use Slash Commands). Do not create a new bot. Wait a minute after the bot comes online. Then restart Discord or wait a short moment for the command list to refresh.

## What gets posted

The bot posts **only the image or video**, with no Reddit link and no link preview. The same picture or video is not posted twice in the same channel, even if Reddit lists both a thumbnail and the full file, or the same media appears under a different post id (for example a crosspost).

## Blocked subreddits

The bot will not add, auto-map, or suggest subreddits whose names are minors-related (for example teen, young, loli, shota, jailbait, underage, child, kid, minor, schoolgirl) or rape/non-consent (for example rape, noncon, non_con, nonconsent, cnc, forced, drugged, sleeping, unaware). Other subreddits are allowed.

## Setup

1. Create a Discord application and bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy the bot token into `.env` as `DISCORD_TOKEN` (see `.env.example`).
3. In the portal, under **Bot**, you do **not** need privileged intents (no Message Content or Server Members). Default gateway intents are enough.
4. Invite the bot with the `bot` and `applications.commands` scopes, and permissions: **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**, and **Use Slash Commands**.
5. In your server, mark each target channel as **Age-restricted** and name it after the subreddit (for example `#aww` → `r/aww`).
6. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.

### Run with Docker

```bash
cp .env.example .env
# edit .env and set DISCORD_TOKEN
docker compose up --build
```

This starts Postgres and the bot. Processed posts are stored in Postgres so the same Reddit post is not sent twice to the same channel. The bot polls on `CHECK_INTERVAL_MINUTES` (default 5) and posts one item per cycle to stay within Reddit rate limits.

### Run without Docker

Install Python 3.10+, install `requirements.txt`, and set `DISCORD_TOKEN` in `.env`. A Postgres database is optional.

If you use Postgres, set `DATABASE_URL` and run the migrations first:

```bash
python migrate_config_settings.py
python migrate_post_status.py
python migrate_multiserver_support.py
python bot.py
```

If you do not set `DATABASE_URL`, the bot uses SQLite automatically. Then just run:

```bash
python bot.py
```

## FadeHost free plan

The FadeHost free plan has no database addon. You do **not** need Postgres or `DATABASE_URL`.

1. Paste your Discord bot token into FadeHost's token field. The host sets `DISCORD_TOKEN` for you.
2. Leave every other environment variable empty. They are all optional.
3. The bot stores its data in a SQLite file on the persistent disk at `/data/bot.db` (3 GB is plenty).

No database, no extra env vars, and no migration commands are required. After the bot comes online, create an age-restricted Discord channel named after the subreddit (for example `#aww` for `r/aww`).

To get an update (for example the `/setup` panel): sync your GitHub fork, then tap **Redeploy** or **Restart** on the FadeHost Overview page. When the bot is back online, restart Discord or wait a minute, then type `/setup` in an age-restricted channel. If `/setup` does not appear, re-invite the **same** bot with the `applications.commands` scope (do not create a new bot).

## Useful env vars

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token (required; FadeHost sets this from the token field) |
| `DATABASE_URL` | Optional Postgres URL. If unset, the bot uses SQLite. |
| `SQLITE_PATH` | Optional SQLite file path. Defaults to `/data/bot.db` when `/data` is writable, otherwise `./bot.db`. |
| `CHECK_INTERVAL_MINUTES` | Poll interval (default `5`) |
| `POST_LIMIT` | Max RSS entries to consider per feed (default `25`) |
| `FEED_MODES` | RSS modes to rotate, e.g. `old,hot,new` |
| `IGNORE_CHANNELS` | Channel names to skip |
| `CHANNEL_SUBREDDIT_MAP` | Manual channel → subreddit overrides |
| `INCLUDE_CAROUSELS` | Include gallery posts for auto-mapped channels (`true`/`false`) |
