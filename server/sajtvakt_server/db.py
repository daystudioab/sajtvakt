"""SQLite: sajter, backuper, jobb, händelser och inställningar."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    key           TEXT NOT NULL,
    added         INTEGER NOT NULL,
    auto_update   INTEGER NOT NULL DEFAULT 1,
    skip_plugins  TEXT NOT NULL DEFAULT '[]',
    status_json   TEXT,
    status_at     INTEGER,
    up            INTEGER,
    fails         INTEGER NOT NULL DEFAULT 0,
    down_since    INTEGER,
    checked_at    INTEGER,
    response_ms   INTEGER,
    ssl_days      INTEGER,
    ssl_checked   INTEGER,
    last_backup   INTEGER,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS backups (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    created   INTEGER NOT NULL,
    kind      TEXT NOT NULL,           -- nightly | pre-update | pre-restore | manual
    status    TEXT NOT NULL,           -- running | done | error
    size      INTEGER NOT NULL DEFAULT 0,
    files     INTEGER NOT NULL DEFAULT 0,
    tables    INTEGER NOT NULL DEFAULT 0,
    path      TEXT,
    note      TEXT,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS backups_site ON backups(site_id, created);
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER,
    kind      TEXT NOT NULL,
    status    TEXT NOT NULL,           -- queued | running | done | error
    created   INTEGER NOT NULL,
    started   INTEGER,
    finished  INTEGER,
    progress  TEXT,
    result    TEXT,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_site ON jobs(site_id, created);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    site_id   INTEGER,
    created   INTEGER NOT NULL,
    level     TEXT NOT NULL,           -- info | ok | warning | error
    title     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS events_site ON events(site_id, created);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token    TEXT PRIMARY KEY,
    created  INTEGER NOT NULL,
    seen     INTEGER NOT NULL,
    csrf     TEXT NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


MIGRATIONS = {
    # kolumn: definition (läggs till i befintliga databaser)
    "endpoint": "TEXT NOT NULL DEFAULT 'admin-post'",
    "rescue_url": "TEXT",
    "maintenance": "INTEGER NOT NULL DEFAULT 0",
    "last_code": "INTEGER",
    "group_name": "TEXT NOT NULL DEFAULT ''",
}


def init() -> None:
    conn().executescript(SCHEMA)
    have = {r["name"] for r in conn().execute("PRAGMA table_info(sites)")}
    for col, definition in MIGRATIONS.items():
        if col not in have:
            conn().execute(f"ALTER TABLE sites ADD COLUMN {col} {definition}")
    if "acked" not in {r["name"] for r in conn().execute("PRAGMA table_info(events)")}:
        conn().execute("ALTER TABLE events ADD COLUMN acked INTEGER NOT NULL DEFAULT 0")
    config.DB_PATH.chmod(0o600)


def q(sql: str, *args) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def one(sql: str, *args) -> sqlite3.Row | None:
    return conn().execute(sql, args).fetchone()


def run(sql: str, *args) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid


# ---------------------------------------------------------------- settings

def setting(key: str) -> str:
    row = one("SELECT value FROM settings WHERE key = ?", key)
    return row["value"] if row else config.DEFAULTS.get(key, "")


def set_setting(key: str, value: str) -> None:
    run("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


# ---------------------------------------------------------------- events & jobs

def event(site_id: int | None, level: str, title: str, detail: str = "") -> None:
    run("INSERT INTO events(site_id, created, level, title, detail) VALUES(?,?,?,?,?)",
        site_id, int(time.time()), level, title, detail)


def job_create(site_id: int | None, kind: str) -> int:
    return run("INSERT INTO jobs(site_id, kind, status, created) VALUES(?,?, 'queued', ?)", site_id, kind, int(time.time()))


def job_update(job_id: int, **fields: Any) -> None:
    if "result" in fields and not isinstance(fields["result"], (str, type(None))):
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE jobs SET {cols} WHERE id = ?", *fields.values(), job_id)


def site(site_id: int) -> sqlite3.Row | None:
    return one("SELECT * FROM sites WHERE id = ?", site_id)


def sites() -> list[sqlite3.Row]:
    return q("SELECT * FROM sites ORDER BY name COLLATE NOCASE")


@contextmanager
def transaction():
    c = conn()
    c.execute("BEGIN")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
