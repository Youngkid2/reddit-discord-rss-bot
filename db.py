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
    conn = sqlite3.connect(sqlite_path)
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


def init_db() -> None:
    if _use_postgres():
        _init_db_postgres()
    else:
        _init_db_sqlite()

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
