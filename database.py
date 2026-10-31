"""Short-lived per-profile cache. Existing results.db schemas are migrated."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_NAME = os.getenv('DATABASE_PATH', str(Path(__file__).with_name('results.db')))
TTL_SECONDS = 180


def connect():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA secure_delete=ON')
    return conn


def purge_expired():
    with connect() as conn:
        cursor = conn.execute('DELETE FROM results WHERE expires_at <= ?', (time.time(),))
        return cursor.rowcount


def init_db():
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,
            followers TEXT, following TEXT, timestamp TEXT,
            expires_at REAL NOT NULL DEFAULT 0
        )''')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(results)')}
        if 'expires_at' not in columns:
            conn.execute('ALTER TABLE results ADD COLUMN expires_at REAL NOT NULL DEFAULT 0')
        # Legacy rows have no trustworthy TTL and are removed during migration.
        conn.execute('DELETE FROM results WHERE expires_at <= ?', (time.time(),))
        conn.execute('CREATE INDEX IF NOT EXISTS idx_results_username ON results(username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_results_expiry ON results(expires_at)')


def save_result(username, followers, following):
    now = time.time()
    expires_at = now + TTL_SECONDS
    with connect() as conn:
        conn.execute('DELETE FROM results WHERE username = ? OR expires_at <= ?', (username, now))
        conn.execute('''INSERT INTO results
            (username, followers, following, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?)''', (
                username, json.dumps(followers[:50], ensure_ascii=False),
                json.dumps(following[:50], ensure_ascii=False),
                datetime.fromtimestamp(now, timezone.utc).isoformat(), expires_at,
            ))
    return expires_at


def get_result_by_username(username):
    now = time.time()
    with connect() as conn:
        conn.execute('DELETE FROM results WHERE expires_at <= ?', (now,))
        row = conn.execute('''SELECT username, followers, following, timestamp, expires_at
            FROM results WHERE username = ? AND expires_at > ?
            ORDER BY expires_at DESC LIMIT 1''', (username, now)).fetchone()
    if row is None:
        return None
    return dict(username=row['username'], followers=json.loads(row['followers']),
                following=json.loads(row['following']), timestamp=row['timestamp'],
                expires_at=row['expires_at'])
