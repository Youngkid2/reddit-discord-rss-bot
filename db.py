import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_backend_announced = False


def _database_url() -> str | None:
    return os.getenv("DATABASE_URL") or None


def _use_postgres() -> bool:
    return bool(_database_url())


def _resolve_sqlite_path() -> str:
    explicit = os.getenv("SQLITE_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    data_dir = Path("/data")
    if data_dir.is_dir() and os.access(data_dir, os.W_OK):
        return str(data_dir / "bot.db")

    return str(Path("bot.db").resolve())


def _announce_backend(kind: str, detail: str | None = None) -> None:
    global _backend_announced
    if _backend_announced:
        return
    _backend_announced = True
    if kind == "postgres":
        print("Database: PostgreSQL")
    else:
        print(f"Database: SQLite ({detail})")


class _SQLiteCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        sql = sql.replace("%s", "?")
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()
        return False


class _SQLiteConnection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return _SQLiteCursor(self._conn.cursor())

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def get_connection():
    if _use_postgres():
        try:
            import psycopg
        except ImportError as error:
            raise ImportError(
                "psycopg is required when DATABASE_URL is set."
            ) from error

        _announce_backend("postgres")
        return psycopg.connect(_database_url())

    sqlite_path = _resolve_sqlite_path()
    _announce_backend("sqlite", sqlite_path)
    conn = sqlite3.connect(sqlite_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as error:
        print(f"SQLite pragma setup failed, continuing with defaults: {error}")
    return _SQLiteConnection(conn)


def _sqlite_columns(cursor, table: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _sqlite_add_column_if_missing(cursor, table: str, column: str, typedef: str) -> None:
    if column not in _sqlite_columns(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


def _init_db_postgres() -> None:
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )


def _init_db_sqlite() -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_posts (
                    reddit_post_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reddit_url TEXT NOT NULL,
                    subreddit_name TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    discord_channel_id TEXT NOT NULL,
                    status TEXT DEFAULT 'posted',
                    skip_reason TEXT,
                    UNIQUE (reddit_post_id, discord_channel_id)
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS subreddit_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subreddit_name TEXT NOT NULL,
                    discord_channel_id TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    include_carousels INTEGER NOT NULL DEFAULT 0,
                    feed_modes TEXT DEFAULT 'old,hot,new',
                    post_limit INTEGER DEFAULT 25,
                    discord_guild_id TEXT,
                    UNIQUE (subreddit_name, discord_channel_id)
                );
                """
            )

            # Upgrade path if an older SQLite file is missing later columns.
            _sqlite_add_column_if_missing(
                cursor, "posted_posts", "discord_channel_id", "TEXT"
            )
            _sqlite_add_column_if_missing(
                cursor, "posted_posts", "status", "TEXT DEFAULT 'posted'"
            )
            _sqlite_add_column_if_missing(
                cursor, "posted_posts", "skip_reason", "TEXT"
            )
            _sqlite_add_column_if_missing(
                cursor, "subreddit_configs", "include_carousels", "INTEGER NOT NULL DEFAULT 0"
            )
            _sqlite_add_column_if_missing(
                cursor, "subreddit_configs", "feed_modes", "TEXT DEFAULT 'old,hot,new'"
            )
            _sqlite_add_column_if_missing(
                cursor, "subreddit_configs", "post_limit", "INTEGER DEFAULT 25"
            )
            _sqlite_add_column_if_missing(
                cursor, "subreddit_configs", "discord_guild_id", "TEXT"
            )

            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS posted_posts_post_channel_uq
                ON posted_posts (reddit_post_id, discord_channel_id);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS subreddit_configs_guild_idx
                ON subreddit_configs (discord_guild_id);
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )


def _ensure_postgres_schema() -> None:
    """Add columns, indexes, and new tables for existing and fresh Postgres DBs."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
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
                ALTER TABLE posted_posts
                DROP CONSTRAINT IF EXISTS posted_posts_pkey;
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS posted_posts_post_channel_uq
                ON posted_posts (reddit_post_id, discord_channel_id);
                """
            )
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
            cursor.execute(
                """
                ALTER TABLE subreddit_configs
                ADD COLUMN IF NOT EXISTS discord_guild_id TEXT;
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS subreddit_configs_guild_idx
                ON subreddit_configs (discord_guild_id);
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_settings (
                    discord_channel_id TEXT PRIMARY KEY,
                    discord_guild_id TEXT,
                    interval_minutes INTEGER,
                    media_type TEXT,
                    feed_modes TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_media (
                    media_key TEXT NOT NULL,
                    discord_channel_id TEXT NOT NULL,
                    reddit_post_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (media_key, discord_channel_id)
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS posted_media_channel_idx
                ON posted_media (discord_channel_id);
                """
            )


def _ensure_sqlite_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_settings (
                    discord_channel_id TEXT PRIMARY KEY,
                    discord_guild_id TEXT,
                    interval_minutes INTEGER,
                    media_type TEXT,
                    feed_modes TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_media (
                    media_key TEXT NOT NULL,
                    discord_channel_id TEXT NOT NULL,
                    reddit_post_id TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE (media_key, discord_channel_id)
                );
                """
            )
            _sqlite_add_column_if_missing(
                cursor, "channel_settings", "discord_guild_id", "TEXT"
            )
            _sqlite_add_column_if_missing(
                cursor, "channel_settings", "interval_minutes", "INTEGER"
            )
            _sqlite_add_column_if_missing(
                cursor, "channel_settings", "media_type", "TEXT"
            )
            _sqlite_add_column_if_missing(
                cursor, "channel_settings", "feed_modes", "TEXT"
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS posted_media_key_channel_uq
                ON posted_media (media_key, discord_channel_id);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS posted_media_channel_idx
                ON posted_media (discord_channel_id);
                """
            )


def init_db() -> None:
    if _use_postgres():
        _init_db_postgres()
        _ensure_postgres_schema()
    else:
        _init_db_sqlite()
        _ensure_sqlite_schema()

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

            rows = cursor.fetchall()

    if _use_postgres():
        return rows

    return [
        (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            bool(row[5]),
        )
        for row in rows
    ]


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
            rows = cursor.fetchall()

    if _use_postgres():
        return rows

    return [
        (
            row[0],
            row[1],
            row[2],
            bool(row[3]),
            row[4],
            row[5],
            bool(row[6]),
            row[7],
        )
        for row in rows
    ]


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

SETTING_INTERVAL = "check_interval_minutes"
SETTING_MEDIA = "media_type"
ALLOWED_INTERVALS = frozenset({1, 2, 5})
ALLOWED_MEDIA_TYPES = frozenset({"videos", "images", "both"})
DEFAULT_MEDIA_TYPE = "both"


def parse_interval(value):
    """Return 1, 2, or 5 if value is one of those, otherwise None."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if minutes in ALLOWED_INTERVALS:
        return minutes
    return None


def normalize_interval(value, default: int = 5) -> int:
    """
    Prefer a valid saved choice (1, 2, or 5).

    If that is missing or invalid, use default (the CHECK_INTERVAL_MINUTES
    env value, which may be any positive integer). Last resort is 5.
    """
    parsed = parse_interval(value)
    if parsed is not None:
        return parsed
    try:
        fallback = int(default)
        if fallback > 0:
            return fallback
    except (TypeError, ValueError):
        pass
    return 5


def normalize_media_type(value, default: str = DEFAULT_MEDIA_TYPE) -> str:
    if isinstance(value, str):
        kind = value.strip().lower()
        if kind in ALLOWED_MEDIA_TYPES:
            return kind
    if isinstance(default, str) and default.strip().lower() in ALLOWED_MEDIA_TYPES:
        return default.strip().lower()
    return DEFAULT_MEDIA_TYPE


def post_matches_media_filter(post: dict, media_type: str | None = None) -> bool:
    """
    Return True if this Reddit post should be posted for the given media filter.

    Video posts often include a thumbnail image, so "images" means image-only
    posts (no video URLs) and "videos" means posts that have a video URL.
    Invalid media_type values fall back to "both".
    """
    kind = normalize_media_type(media_type)
    image_urls = post.get("image_urls") or []
    video_urls = post.get("video_urls") or []

    if kind == "videos":
        return bool(video_urls)
    if kind == "images":
        return bool(image_urls) and not video_urls
    return bool(image_urls or video_urls)


def _ensure_settings_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def get_bot_setting(key: str, default: str | None = None) -> str | None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                _ensure_settings_table(cursor)
                cursor.execute(
                    """
                    SELECT value
                    FROM bot_settings
                    WHERE key = %s;
                    """,
                    (key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return default
                return row[0]
    except Exception as error:
        print(f"Failed to read setting {key}, using default: {error}")
        return default


def set_bot_setting(key: str, value: str) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                _ensure_settings_table(cursor)
                cursor.execute(
                    """
                    INSERT INTO bot_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value;
                    """,
                    (key, value),
                )
        return True
    except Exception as error:
        print(f"Failed to save setting {key}: {error}")
        return False


def get_check_interval_minutes(default: int = 5) -> int:
    try:
        return normalize_interval(get_bot_setting(SETTING_INTERVAL), default=default)
    except Exception as error:
        print(f"Failed to load check interval, using default: {error}")
        return normalize_interval(None, default=default)


def get_media_type(default: str = DEFAULT_MEDIA_TYPE) -> str:
    try:
        return normalize_media_type(get_bot_setting(SETTING_MEDIA), default=default)
    except Exception as error:
        print(f"Failed to load media type, using default: {error}")
        return normalize_media_type(None, default=default)


def set_check_interval_minutes(minutes: int) -> bool:
    parsed = parse_interval(minutes)
    if parsed is None:
        return False
    return set_bot_setting(SETTING_INTERVAL, str(parsed))


def set_media_type(media_type: str) -> bool:
    if not isinstance(media_type, str):
        return False
    kind = media_type.strip().lower()
    if kind not in ALLOWED_MEDIA_TYPES:
        return False
    return set_bot_setting(SETTING_MEDIA, kind)



def _row_channel_settings(row) -> dict | None:
    if not row:
        return None
    interval = row[1]
    try:
        interval = int(interval) if interval is not None else None
    except (TypeError, ValueError):
        interval = None
    return {
        "discord_channel_id": row[0],
        "interval_minutes": interval,
        "media_type": row[2],
        "feed_modes": row[3],
        "discord_guild_id": row[4],
    }


def get_channel_settings(discord_channel_id: str) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    discord_channel_id,
                    interval_minutes,
                    media_type,
                    feed_modes,
                    discord_guild_id
                FROM channel_settings
                WHERE discord_channel_id = %s;
                """,
                (str(discord_channel_id),),
            )
            return _row_channel_settings(cursor.fetchone())


def list_channel_settings(discord_guild_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if discord_guild_id:
                cursor.execute(
                    """
                    SELECT
                        discord_channel_id,
                        interval_minutes,
                        media_type,
                        feed_modes,
                        discord_guild_id
                    FROM channel_settings
                    WHERE discord_guild_id = %s
                    ORDER BY discord_channel_id ASC;
                    """,
                    (str(discord_guild_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        discord_channel_id,
                        interval_minutes,
                        media_type,
                        feed_modes,
                        discord_guild_id
                    FROM channel_settings
                    ORDER BY discord_channel_id ASC;
                    """
                )
            return [
                settings
                for settings in (_row_channel_settings(row) for row in cursor.fetchall())
                if settings
            ]


def upsert_channel_settings(
    discord_channel_id: str,
    discord_guild_id: str | None = None,
    interval_minutes: int | None = None,
    media_type: str | None = None,
    feed_modes: str | None = None,
    overwrite: bool = False,
) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                if overwrite:
                    cursor.execute(
                        """
                        INSERT INTO channel_settings (
                            discord_channel_id,
                            discord_guild_id,
                            interval_minutes,
                            media_type,
                            feed_modes
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (discord_channel_id)
                        DO UPDATE SET
                            discord_guild_id = COALESCE(EXCLUDED.discord_guild_id, channel_settings.discord_guild_id),
                            interval_minutes = EXCLUDED.interval_minutes,
                            media_type = EXCLUDED.media_type,
                            feed_modes = EXCLUDED.feed_modes;
                        """,
                        (
                            str(discord_channel_id),
                            discord_guild_id,
                            interval_minutes,
                            media_type,
                            feed_modes,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO channel_settings (
                            discord_channel_id,
                            discord_guild_id,
                            interval_minutes,
                            media_type,
                            feed_modes
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (discord_channel_id)
                        DO UPDATE SET
                            discord_guild_id = COALESCE(EXCLUDED.discord_guild_id, channel_settings.discord_guild_id),
                            interval_minutes = COALESCE(EXCLUDED.interval_minutes, channel_settings.interval_minutes),
                            media_type = COALESCE(EXCLUDED.media_type, channel_settings.media_type),
                            feed_modes = COALESCE(EXCLUDED.feed_modes, channel_settings.feed_modes);
                        """,
                        (
                            str(discord_channel_id),
                            discord_guild_id,
                            interval_minutes,
                            media_type,
                            feed_modes,
                        ),
                    )
        return True
    except Exception as error:
        print(f"Failed to save channel settings for {discord_channel_id}: {error}")
        return False


def list_channel_intervals() -> list[int]:
    settings = list_channel_settings()
    values = []
    for row in settings:
        minutes = parse_interval(row.get("interval_minutes"))
        if minutes is not None:
            values.append(minutes)
    return values


def replace_channel_subreddit_configs(
    discord_channel_id: str,
    discord_guild_id: str | None,
    subreddit_names: list[str],
    feed_modes: str = "old,hot,new",
    post_limit: int = 25,
) -> list[str]:
    """Replace the saved subreddit mappings for one channel."""
    channel_id = str(discord_channel_id)
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM subreddit_configs
                WHERE discord_channel_id = %s;
                """,
                (channel_id,),
            )
    saved = []
    for name in subreddit_names:
        upsert_subreddit_config(
            subreddit_name=name,
            discord_channel_id=channel_id,
            feed_modes=feed_modes,
            post_limit=post_limit,
            discord_guild_id=discord_guild_id,
        )
        saved.append(name)
    return saved


def has_media_been_processed(media_key: str, discord_channel_id: str) -> bool:
    if not media_key:
        return False
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM posted_media
                WHERE media_key = %s
                AND discord_channel_id = %s;
                """,
                (media_key, str(discord_channel_id)),
            )
            return cursor.fetchone() is not None


def already_posted_to_channel(post_id: str, discord_channel_id: str, media_keys: list[str] | None = None) -> bool:
    if has_been_processed(post_id, discord_channel_id):
        return True
    for key in media_keys or []:
        if has_media_been_processed(key, discord_channel_id):
            return True
    return False


def claim_post_for_channel(
    post_id: str,
    title: str,
    reddit_url: str,
    subreddit_name: str,
    discord_channel_id: str,
    media_keys: list[str] | None = None,
) -> bool:
    """
    Atomically reserve a Reddit post and its media for this channel.

    Returns False if the post id or any media key is already claimed.
    The row is stored as status='pending' until mark_as_posted() or
    release_claim() runs.
    """
    channel_id = str(discord_channel_id)
    keys = []
    seen = set()
    for key in media_keys or []:
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                for key in keys:
                    cursor.execute(
                        """
                        SELECT 1
                        FROM posted_media
                        WHERE media_key = %s
                        AND discord_channel_id = %s;
                        """,
                        (key, channel_id),
                    )
                    if cursor.fetchone() is not None:
                        conn.rollback()
                        return False

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
                    DO NOTHING;
                    """,
                    (
                        post_id,
                        title,
                        reddit_url,
                        subreddit_name,
                        channel_id,
                        "pending",
                        None,
                    ),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return False

                for key in keys:
                    cursor.execute(
                        """
                        INSERT INTO posted_media (
                            media_key,
                            discord_channel_id,
                            reddit_post_id
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (media_key, discord_channel_id)
                        DO NOTHING;
                        """,
                        (key, channel_id, post_id),
                    )
                    if cursor.rowcount == 0:
                        conn.rollback()
                        return False
        return True
    except Exception as error:
        print(
            f"Failed to claim post {post_id} for channel {channel_id}: {error}"
        )
        return False


def release_claim(
    post_id: str,
    discord_channel_id: str,
    media_keys: list[str] | None = None,
) -> None:
    """Drop a pending claim so the post can be retried later."""
    channel_id = str(discord_channel_id)
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM posted_posts
                    WHERE reddit_post_id = %s
                    AND discord_channel_id = %s
                    AND status = %s;
                    """,
                    (post_id, channel_id, "pending"),
                )
                cursor.execute(
                    """
                    DELETE FROM posted_media
                    WHERE reddit_post_id = %s
                    AND discord_channel_id = %s;
                    """,
                    (post_id, channel_id),
                )
                for key in media_keys or []:
                    cursor.execute(
                        """
                        DELETE FROM posted_media
                        WHERE media_key = %s
                        AND discord_channel_id = %s;
                        """,
                        (key, channel_id),
                    )
    except Exception as error:
        print(
            f"Failed to release claim for {post_id} in channel {channel_id}: {error}"
        )
