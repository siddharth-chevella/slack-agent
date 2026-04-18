"""
Database persistence for OLake Slack Community Agent.

PostgreSQL only (via ``DATABASE_URL``). Use the ``postgres`` service in
docker-compose or any reachable Postgres instance.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from agent.logger import get_logger

MISSING_DATABASE_URL = (
    "DATABASE_URL is required. The agent persists data to PostgreSQL only "
    "(for example the `postgres` service in docker-compose). "
    "Set DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE"
)


def _postgres_dsn_from_url(url: str) -> str:
    """Normalize connection string for psycopg (accepts postgresql:// or postgres://)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError(
            f"DATABASE_URL must use postgresql:// or postgres:// scheme, got {parsed.scheme!r}"
        )
    return url


def _connection_target_label(dsn: str) -> tuple[str, str]:
    """Return (host:port, database_name) for error messages."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "?"
    port = parsed.port or 5432
    db_name = (parsed.path or "/").lstrip("/") or "?"
    return f"{host}:{port}", db_name


def _is_database_does_not_exist(exc: BaseException) -> bool:
    """True when Postgres reports the catalog in the URL does not exist (SQLSTATE 3D000)."""
    from psycopg.errors import InvalidCatalogName

    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, InvalidCatalogName):
            return True
        if getattr(e, "sqlstate", None) == "3D000":
            return True
        e = e.__cause__ or e.__context__
    return False


class Database:
    """PostgreSQL persistence."""

    def __init__(self, database_url: str) -> None:
        self.logger = get_logger()
        url = (database_url or "").strip()
        if not url:
            raise RuntimeError(MISSING_DATABASE_URL)
        self._dsn = _postgres_dsn_from_url(url)
        self._init_database()

    @contextmanager
    def get_connection(self):
        import psycopg
        from psycopg import Connection

        conn: Connection = psycopg.connect(self._dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_connection(self) -> None:
        """
        Verify the database is reachable and accepting queries.

        Raises:
            RuntimeError: Human-readable message (missing database vs other failures).
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception as exc:
            hostport, db_name = _connection_target_label(self._dsn)
            if _is_database_does_not_exist(exc):
                raise RuntimeError(
                    f"PostgreSQL database {db_name!r} does not exist on {hostport!r}. "
                    "Create that database (for Docker Compose, ensure POSTGRES_DB matches the "
                    "database name in DATABASE_URL) or fix DATABASE_URL. "
                    f"Original error: {exc}"
                ) from exc
            raise RuntimeError(
                f"Cannot connect to PostgreSQL at {hostport!r} (database {db_name!r}): {exc}\n"
                "Ensure the server is running and DATABASE_URL is correct."
            ) from exc

    def _init_database(self) -> None:
        """Create normalized schema (idempotent)."""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            user_id      TEXT PRIMARY KEY,
                            username     TEXT,
                            display_name TEXT,
                            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            last_seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS threads (
                            thread_id  TEXT PRIMARY KEY,
                            channel_id TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS messages (
                            id         BIGSERIAL PRIMARY KEY,
                            thread_id  TEXT NOT NULL,
                            user_id    TEXT,
                            role       TEXT NOT NULL CHECK(role IN ('user', 'agent')),
                            content    TEXT NOT NULL,
                            message_ts TEXT NOT NULL UNIQUE,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_messages_thread
                        ON messages(thread_id, message_ts ASC)
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_messages_user
                        ON messages(user_id, message_ts DESC)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS thread_summaries (
                            thread_id              TEXT PRIMARY KEY,
                            summary                TEXT NOT NULL,
                            summarised_through_ts  TEXT NOT NULL,
                            updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
        except Exception as exc:
            if _is_database_does_not_exist(exc):
                hostport, db_name = _connection_target_label(self._dsn)
                raise RuntimeError(
                    f"PostgreSQL database {db_name!r} does not exist on {hostport!r}. "
                    "Create that database or fix DATABASE_URL. "
                    f"Original error: {exc}"
                ) from exc
            raise

        self.logger.logger.debug("Persistence database (PostgreSQL) ready")

    def upsert_user(
        self,
        user_id: str,
        username: str = "",
        display_name: str = "",
    ) -> None:
        """Insert or update a user record; always touches last_seen_at."""
        sql = """
                INSERT INTO users (user_id, username, display_name, first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username     = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    last_seen_at = CURRENT_TIMESTAMP
            """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (user_id, username, display_name))

    def upsert_thread(self, thread_id: str, channel_id: str) -> None:
        """Insert or update a thread record; always touches updated_at."""
        sql = """
                INSERT INTO threads (thread_id, channel_id, created_at, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(thread_id) DO UPDATE SET
                    updated_at = CURRENT_TIMESTAMP
            """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (thread_id, channel_id))

    def save_message(
        self,
        thread_id: str,
        user_id: Optional[str],
        role: str,
        content: str,
        message_ts: str,
    ) -> None:
        """Insert a single message row. Silently ignores duplicate message_ts."""
        sql = """
                INSERT INTO messages
                    (thread_id, user_id, role, content, message_ts)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (message_ts) DO NOTHING
            """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (thread_id, user_id, role, content, message_ts))

    def get_thread_messages(
        self,
        thread_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return messages for a thread in chronological order (ASC by message_ts).
        When limit is given, returns the last `limit` messages.
        """
        from psycopg.rows import dict_row

        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if limit:
                    cur.execute(
                        """
                        SELECT * FROM (
                            SELECT id, thread_id, user_id, role, content, message_ts, created_at
                            FROM messages
                            WHERE thread_id = %s
                            ORDER BY message_ts DESC
                            LIMIT %s
                        ) AS t ORDER BY message_ts ASC
                        """,
                        (thread_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, thread_id, user_id, role, content, message_ts, created_at
                        FROM messages
                        WHERE thread_id = %s
                        ORDER BY message_ts ASC
                        """,
                        (thread_id,),
                    )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_thread_messages_after(
        self,
        thread_id: str,
        after_ts: str,
    ) -> List[Dict[str, Any]]:
        """
        Return messages for a thread with message_ts > after_ts, ASC.
        Pass after_ts="" or "0" to get all messages (when no prior summary exists).
        """
        from psycopg.rows import dict_row

        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, thread_id, user_id, role, content, message_ts, created_at
                    FROM messages
                    WHERE thread_id = %s AND message_ts > %s
                    ORDER BY message_ts ASC
                    """,
                    (thread_id, after_ts),
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_user_messages(
        self,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return all messages by a user across all threads, most recent first."""
        from psycopg.rows import dict_row

        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, thread_id, user_id, role, content, message_ts, created_at
                    FROM messages
                    WHERE user_id = %s
                    ORDER BY message_ts DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_thread_summary(self, thread_id: str) -> Tuple[Optional[str], str]:
        """
        Return (summary_text, summarised_through_ts).
        If no row exists, returns (None, "").
        """
        from psycopg.rows import dict_row

        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT summary, summarised_through_ts
                    FROM thread_summaries
                    WHERE thread_id = %s
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
        if row:
            d = dict(row)
            return d["summary"], d["summarised_through_ts"]
        return None, ""

    def upsert_thread_summary(
        self,
        thread_id: str,
        summary: str,
        summarised_through_ts: str,
    ) -> None:
        """Insert or overwrite the summary for a thread."""
        sql = """
                INSERT INTO thread_summaries
                    (thread_id, summary, summarised_through_ts, updated_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT(thread_id) DO UPDATE SET
                    summary               = EXCLUDED.summary,
                    summarised_through_ts = EXCLUDED.summarised_through_ts,
                    updated_at            = CURRENT_TIMESTAMP
            """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (thread_id, summary, summarised_through_ts))

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate counts for /stats and CLI --stats."""
        from psycopg.rows import dict_row

        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT thread_id) AS total_threads,
                           COUNT(*) AS total_messages,
                           COUNT(DISTINCT user_id) AS unique_users
                    FROM messages
                    """
                )
                stats = dict(cur.fetchone() or {})

                cur.execute(
                    "SELECT COUNT(*) AS agent_messages FROM messages WHERE role = 'agent'"
                )
                row = cur.fetchone()
                stats["agent_messages"] = dict(row)["agent_messages"] if row else 0

        return {
            "total_threads": stats.get("total_threads") or 0,
            "total_messages": stats.get("total_messages") or 0,
            "agent_messages": stats.get("agent_messages") or 0,
            "unique_users": stats.get("unique_users") or 0,
        }


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_db: Optional[Database] = None


def get_database(database_url: Optional[str] = None) -> Database:
    """
    Return the process-global :class:`Database` instance.

    ``DATABASE_URL`` must be set (PostgreSQL). Raises :exc:`RuntimeError` if unset.
    """
    global _db
    if _db is None:
        from agent.config import Config

        url = (database_url if database_url is not None else Config.DATABASE_URL) or ""
        url = url.strip()
        if not url:
            raise RuntimeError(MISSING_DATABASE_URL)
        _db = Database(database_url=url)
    return _db
