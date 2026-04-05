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
    """Create all tables if they don't exist. Call once at startup."""
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_drafts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT    NOT NULL,
                from_name           TEXT    NOT NULL DEFAULT '',
                from_email          TEXT    NOT NULL DEFAULT '',
                subject             TEXT    NOT NULL DEFAULT '',
                customer_message    TEXT    NOT NULL DEFAULT '',
                ai_reply_html       TEXT    NOT NULL DEFAULT '',
                gmail_draft_id      TEXT    NOT NULL DEFAULT '',
                original_email_id   TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_created ON email_drafts(created_at)")


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


# ---------------------------------------------------------------------------
# Email drafts — Write
# ---------------------------------------------------------------------------

def log_email_draft(
    from_name: str,
    from_email: str,
    subject: str,
    customer_message: str,
    ai_reply_html: str,
    gmail_draft_id: str,
    original_email_id: str,
) -> None:
    """Persist a single email draft record."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO email_drafts
                (created_at, from_name, from_email, subject, customer_message,
                 ai_reply_html, gmail_draft_id, original_email_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                from_name,
                from_email,
                subject,
                customer_message,
                ai_reply_html,
                gmail_draft_id,
                original_email_id,
            ),
        )


# ---------------------------------------------------------------------------
# Email drafts — Read
# ---------------------------------------------------------------------------

def get_email_drafts(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
) -> list[dict]:
    """Return email drafts newest-first, optionally filtered by keyword."""
    with _connect() as conn:
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                """
                SELECT id, created_at, from_name, from_email, subject,
                       customer_message, ai_reply_html, gmail_draft_id, original_email_id
                FROM email_drafts
                WHERE from_name LIKE ? OR from_email LIKE ? OR subject LIKE ?
                   OR customer_message LIKE ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, like, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, created_at, from_name, from_email, subject,
                       customer_message, ai_reply_html, gmail_draft_id, original_email_id
                FROM email_drafts
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_email_draft_count(search: str = "") -> int:
    """Total number of email draft records (optionally filtered)."""
    with _connect() as conn:
        if search:
            like = f"%{search}%"
            row = conn.execute(
                """
                SELECT COUNT(*) FROM email_drafts
                WHERE from_name LIKE ? OR from_email LIKE ? OR subject LIKE ?
                   OR customer_message LIKE ?
                """,
                (like, like, like, like),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM email_drafts").fetchone()
    return row[0]


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
