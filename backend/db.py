"""
db.py — Conversation logging to SQLite.

Used by main.py to persist every chat exchange for review in the admin panel.
The database file lives at DATA_DIR/conversations.db (defaults to /app/data on
Railway; falls back to a local .tmp/ directory for development).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent.parent / ".tmp")))
DB_PATH = _DATA_DIR / "conversations.db"


def init_db() -> None:
    """Create the conversations table if it doesn't exist. Call once at startup."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL,
                user_message  TEXT  NOT NULL,
                bot_response  TEXT  NOT NULL,
                sources     TEXT    NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations(timestamp)")


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def log_conversation(
    session_id: str,
    user_message: str,
    bot_response: str,
    sources: list[dict],
) -> None:
    """Persist a single chat exchange."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversations (session_id, timestamp, user_message, bot_response, sources)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                datetime.now(timezone.utc).isoformat(),
                user_message,
                bot_response,
                json.dumps(sources),
            ),
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_conversations(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
) -> list[dict]:
    """Return conversations newest-first, optionally filtered by keyword."""
    with _connect() as conn:
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, user_message, bot_response, sources
                FROM conversations
                WHERE user_message LIKE ? OR bot_response LIKE ? OR session_id LIKE ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, user_message, bot_response, sources
                FROM conversations
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_conversation_count(search: str = "") -> int:
    """Total number of logged exchanges (optionally filtered)."""
    with _connect() as conn:
        if search:
            like = f"%{search}%"
            row = conn.execute(
                """
                SELECT COUNT(*) FROM conversations
                WHERE user_message LIKE ? OR bot_response LIKE ? OR session_id LIKE ?
                """,
                (like, like, like),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
    return row[0]
