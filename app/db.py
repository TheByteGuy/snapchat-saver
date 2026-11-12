"""SQLite storage. Synchronous, one connection per call, safe from any thread."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipients (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fact       TEXT NOT NULL,
    image_path TEXT NOT NULL,
    status     TEXT NOT NULL,
    results    TEXT NOT NULL DEFAULT '{}',
    test       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_date ON history(date);
CREATE TABLE IF NOT EXISTS used_facts (
    fact    TEXT PRIMARY KEY,
    used_at TEXT NOT NULL
);
"""


def _defaults() -> dict[str, str]:
    return {
        "send_time": "09:00",
        "timezone": get_settings().DEFAULT_TZ,
        "enabled": "1",
        "discord_webhook_url": "",
        "notify_on_success": "1",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(get_settings().DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        with conn:  # commits on success, rolls back on exception
            yield conn
    finally:
        conn.close()


_initialized: set[str] = set()


def init_db() -> None:
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
    _initialized.add(str(get_settings().DB_PATH))


def _ensure() -> None:
    path = get_settings().DB_PATH
    if str(path) not in _initialized or not path.exists():
        init_db()


# --------------------------------------------------------------------- settings
def get_setting(key: str, default: str | None = None) -> str | None:
    _ensure()
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    if default is not None:
        return default
    return _defaults().get(key)


def set_setting(key: str, value: str) -> None:
    _ensure()
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# ------------------------------------------------------------------- recipients
def _recipient(row: sqlite3.Row) -> dict:
    return {"id": int(row["id"]), "name": row["name"], "enabled": bool(row["enabled"])}


def list_recipients() -> list[dict]:
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT id, name, enabled FROM recipients ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_recipient(r) for r in rows]


def add_recipient(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("recipient name must not be empty")
    _ensure()
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO recipients(name, enabled) VALUES(?, 1)", (name,))
        row = c.execute("SELECT id, name, enabled FROM recipients WHERE name = ?", (name,)).fetchone()
    return _recipient(row)


def remove_recipient(id: int) -> None:
    _ensure()
    with _conn() as c:
        c.execute("DELETE FROM recipients WHERE id = ?", (int(id),))


def set_recipient_enabled(id: int, enabled: bool) -> None:
    _ensure()
    with _conn() as c:
        c.execute("UPDATE recipients SET enabled = ? WHERE id = ?", (1 if enabled else 0, int(id)))


# ---------------------------------------------------------------------- history
def add_history(
    date: str,
    fact: str,
    image_path: str,
    status: str,
    results: dict,
    test: bool = False,
) -> int:
    _ensure()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO history(date, created_at, fact, image_path, status, results, test) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (date, _now_iso(), fact, str(image_path), status, json.dumps(results or {}), 1 if test else 0),
        )
        return int(cur.lastrowid)


def list_history(limit: int = 30) -> list[dict]:
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        try:
            results = json.loads(r["results"] or "{}")
        except (TypeError, ValueError):
            results = {}
        out.append(
            {
                "id": int(r["id"]),
                "date": r["date"],
                "created_at": r["created_at"],
                "fact": r["fact"],
                "image_path": r["image_path"],
                "status": r["status"],
                "results": results,
                "test": bool(r["test"]),
            }
        )
    return out


def sent_today(date: str) -> bool:
    _ensure()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM history WHERE date = ? AND test = 0 AND status IN ('success', 'partial') LIMIT 1",
            (date,),
        ).fetchone()
    return row is not None


# ------------------------------------------------------------------------ facts
def _norm_fact(fact: str) -> str:
    return " ".join((fact or "").split()).strip()


def fact_used(fact: str) -> bool:
    _ensure()
    with _conn() as c:
        row = c.execute("SELECT 1 FROM used_facts WHERE fact = ?", (_norm_fact(fact),)).fetchone()
    return row is not None


def mark_fact_used(fact: str) -> None:
    _ensure()
    with _conn() as c:
        c.execute(
            "INSERT INTO used_facts(fact, used_at) VALUES(?, ?) "
            "ON CONFLICT(fact) DO UPDATE SET used_at = excluded.used_at",
            (_norm_fact(fact), _now_iso()),
        )


def fact_last_used(fact: str) -> str | None:
    """ISO timestamp the fact was last used, or None (helper for facts.py)."""
    _ensure()
    with _conn() as c:
        row = c.execute("SELECT used_at FROM used_facts WHERE fact = ?", (_norm_fact(fact),)).fetchone()
    return row["used_at"] if row else None


def used_facts_map() -> dict[str, str]:
    """All used facts -> used_at ISO timestamp (helper for facts.py)."""
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT fact, used_at FROM used_facts").fetchall()
    return {r["fact"]: r["used_at"] for r in rows}
