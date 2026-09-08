"""SQLite data layer for the electricity usage bot.

All data is scoped per Telegram user_id, so multiple people can use the same
bot instance with completely separate meter trees, readings, and period
settings.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "meters.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES meters(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    meter_id INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    value REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_readings_meter_time ON readings(meter_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_readings_user ON readings(user_id);
CREATE INDEX IF NOT EXISTS idx_meters_user_parent ON meters(user_id, parent_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- settings (per user) ----------

def get_setting(user_id: int, key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
        return row["value"] if row else None


def set_setting(user_id: int, key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, key, value),
        )


# ---------- meters ----------
# Every read/write is scoped to user_id, so one user can never see or touch
# another user's meters, even if they somehow guessed a numeric meter id.

def add_meter(user_id: int, name: str, parent_id: int | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meters(user_id, name, parent_id, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, parent_id, now_iso()),
        )
        return cur.lastrowid


def get_meter(meter_id: int, user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM meters WHERE id=? AND user_id=?", (meter_id, user_id)
        ).fetchone()


def list_meters(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM meters WHERE user_id=? ORDER BY name COLLATE NOCASE", (user_id,)
        ).fetchall()


def get_children(user_id: int, parent_id: int | None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if parent_id is None:
            return conn.execute(
                "SELECT * FROM meters WHERE user_id=? AND parent_id IS NULL "
                "ORDER BY name COLLATE NOCASE",
                (user_id,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM meters WHERE user_id=? AND parent_id=? ORDER BY name COLLATE NOCASE",
            (user_id, parent_id),
        ).fetchall()


def rename_meter(meter_id: int, user_id: int, new_name: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE meters SET name=? WHERE id=? AND user_id=?", (new_name, meter_id, user_id)
        )


def move_meter(meter_id: int, user_id: int, new_parent_id: int | None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE meters SET parent_id=? WHERE id=? AND user_id=?",
            (new_parent_id, meter_id, user_id),
        )


def delete_meter(meter_id: int, user_id: int):
    """Deletes the meter (if owned by user_id) and cascades to descendants + their readings."""
    with get_conn() as conn:
        conn.execute("DELETE FROM meters WHERE id=? AND user_id=?", (meter_id, user_id))


def is_descendant(candidate_id: int, of_id: int, user_id: int) -> bool:
    """True if `candidate_id` is `of_id` itself or a descendant of it (used to block cycles)."""
    with get_conn() as conn:
        frontier = [of_id]
        seen = set()
        while frontier:
            current = frontier.pop()
            if current == candidate_id:
                return True
            seen.add(current)
            rows = conn.execute(
                "SELECT id FROM meters WHERE parent_id=? AND user_id=?", (current, user_id)
            ).fetchall()
            frontier.extend(r["id"] for r in rows if r["id"] not in seen)
        return False


# ---------- readings ----------

def add_reading(
    meter_id: int, user_id: int, value: float, recorded_at: str | None = None, note: str | None = None
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings(user_id, meter_id, value, recorded_at, note) VALUES (?, ?, ?, ?, ?)",
            (user_id, meter_id, value, recorded_at or now_iso(), note),
        )
        return cur.lastrowid


def get_last_reading_at_or_before(meter_id: int, user_id: int, dt_iso: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM readings WHERE meter_id=? AND user_id=? AND recorded_at<=? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (meter_id, user_id, dt_iso),
        ).fetchone()


def get_first_reading_at_or_after(meter_id: int, user_id: int, dt_iso: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM readings WHERE meter_id=? AND user_id=? AND recorded_at>=? "
            "ORDER BY recorded_at ASC LIMIT 1",
            (meter_id, user_id, dt_iso),
        ).fetchone()


def get_recent_readings(meter_id: int, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM readings WHERE meter_id=? AND user_id=? ORDER BY recorded_at DESC LIMIT ?",
            (meter_id, user_id, limit),
        ).fetchall()


def delete_reading(reading_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM readings WHERE id=? AND user_id=?", (reading_id, user_id))


def get_last_reading(meter_id: int, user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM readings WHERE meter_id=? AND user_id=? ORDER BY recorded_at DESC LIMIT 1",
            (meter_id, user_id),
        ).fetchone()
