"""Database layer for vocabulary storage and review tracking.

Supports SQLite (local dev) and PostgreSQL (production via DATABASE_URL).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "greek.db"

# Detect database backend at module level
_database_url = os.getenv("DATABASE_URL", "")


def _is_postgres() -> bool:
    return _database_url.startswith("postgres")


def get_connection():
    """Return a database connection (SQLite or PostgreSQL)."""
    if _is_postgres():
        import psycopg2
        import psycopg2.extras
        # Render uses postgres:// but psycopg2 needs postgresql://
        url = _database_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ph(sql: str) -> str:
    """Convert ? placeholders to %s for PostgreSQL."""
    if _is_postgres():
        return sql.replace("?", "%s")
    return sql


def _row_to_dict(row, cursor_description=None) -> dict:
    """Convert a database row to a dict, handling both SQLite Row and psycopg2 tuples."""
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if cursor_description and isinstance(row, tuple):
        return {desc[0]: val for desc, val in zip(cursor_description, row)}
    return dict(row)


def fetchall_dicts(conn, sql: str, params=()) -> list[dict]:
    """Execute a query and return results as list of dicts (works for both backends)."""
    sql = ph(sql)
    if _is_postgres():
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetchone_dict(conn, sql: str, params=()) -> dict | None:
    """Execute a query and return one result as a dict."""
    sql = ph(sql)
    if _is_postgres():
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None

    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(conn, sql: str, params=()):
    """Execute a statement with placeholder conversion."""
    sql = ph(sql)
    if _is_postgres():
        cur = conn.cursor()
        cur.execute(sql, params)
        cur.close()
    else:
        conn.execute(sql, params)


def init_db() -> None:
    """Create all tables if they don't exist."""
    conn = get_connection()

    if _is_postgres():
        _init_postgres(conn)
    else:
        _init_sqlite(conn)

    conn.commit()
    conn.close()


def _init_sqlite(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            greek       TEXT NOT NULL,
            english     TEXT NOT NULL,
            part_of_speech TEXT,
            example_el  TEXT,
            example_en  TEXT,
            tags        TEXT,  -- comma-separated
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id     INTEGER NOT NULL REFERENCES words(id),
            reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
            quality     INTEGER NOT NULL CHECK (quality BETWEEN 0 AND 5),
            -- SM-2 state after this review
            ease_factor REAL NOT NULL,
            interval    REAL NOT NULL,  -- days
            repetition  INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_words_greek
            ON words(greek);

        CREATE INDEX IF NOT EXISTS idx_reviews_word
            ON reviews(word_id, reviewed_at);

        -- Conversation log
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            direction       TEXT NOT NULL CHECK (direction IN ('out', 'in')),
            body            TEXT NOT NULL,
            telegram_msg_id INTEGER,
            target_word_ids TEXT,  -- JSON array of word IDs
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Interests learned from conversation
        CREATE TABLE IF NOT EXISTS profile_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Send tracking (prevents duplicates, tracks daily count)
        CREATE TABLE IF NOT EXISTS send_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_date   TEXT NOT NULL,  -- YYYY-MM-DD
            sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
            message_id  INTEGER REFERENCES messages(id)
        );

        CREATE INDEX IF NOT EXISTS idx_send_log_date
            ON send_log(sent_date);

        CREATE INDEX IF NOT EXISTS idx_messages_direction
            ON messages(direction, created_at);
    """)


def _init_postgres(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id              SERIAL PRIMARY KEY,
            greek           TEXT NOT NULL,
            english         TEXT NOT NULL,
            part_of_speech  TEXT,
            example_el      TEXT,
            example_en      TEXT,
            tags            TEXT,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_words_greek
            ON words(greek);

        CREATE TABLE IF NOT EXISTS reviews (
            id              SERIAL PRIMARY KEY,
            word_id         INTEGER NOT NULL REFERENCES words(id),
            reviewed_at     TIMESTAMP NOT NULL DEFAULT NOW(),
            quality         INTEGER NOT NULL CHECK (quality BETWEEN 0 AND 5),
            ease_factor     DOUBLE PRECISION NOT NULL,
            interval        DOUBLE PRECISION NOT NULL,
            repetition      INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_reviews_word
            ON reviews(word_id, reviewed_at);

        CREATE TABLE IF NOT EXISTS messages (
            id              SERIAL PRIMARY KEY,
            direction       TEXT NOT NULL CHECK (direction IN ('out', 'in')),
            body            TEXT NOT NULL,
            telegram_msg_id INTEGER,
            target_word_ids TEXT,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS profile_notes (
            id              SERIAL PRIMARY KEY,
            category        TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS send_log (
            id              SERIAL PRIMARY KEY,
            sent_date       TEXT NOT NULL,
            sent_at         TIMESTAMP NOT NULL DEFAULT NOW(),
            message_id      INTEGER REFERENCES messages(id)
        );

        CREATE INDEX IF NOT EXISTS idx_send_log_date
            ON send_log(sent_date);

        CREATE INDEX IF NOT EXISTS idx_messages_direction
            ON messages(direction, created_at);
    """)
    cur.close()
