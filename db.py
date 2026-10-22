"""Database access.

SQLite on a Railway persistent volume. DB_PATH must point at the volume
mount or the database is wiped on every redeploy.
"""

import json
import os
import sqlite3
import secrets
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/meals.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns stored as JSON text, decoded on read.
JSON_COLUMNS = {
    "aliases", "unit_conversions", "tags", "nutrition",
    "substeps", "ingredient_refs", "allergens", "tools",
}


def _row_factory(cursor, row):
    out = {}
    for idx, col in enumerate(cursor.description):
        name = col[0]
        value = row[idx]
        if name in JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        out[name] = value
    return out


# SQLite fails a write immediately if another write holds the lock — the
# default busy timeout is zero. With several requests in flight (ingesting
# while mirroring images, say) that surfaces as random 500s that succeed on
# retry. WAL lets readers and a writer coexist; busy_timeout makes a waiting
# writer wait instead of giving up.
BUSY_TIMEOUT_MS = 10_000
_wal_enabled = False


@contextmanager
def connect():
    global _wal_enabled
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not _wal_enabled:
        conn.execute("PRAGMA journal_mode = WAL")
        _wal_enabled = True
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate():
    """Apply schema.sql, then add any columns missing from existing tables.

    schema.sql is all CREATE TABLE IF NOT EXISTS, so it creates new tables
    but never alters existing ones. Anything added to a table after the
    database already exists must be listed in ADDED_COLUMNS as well.
    """
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text())

        for table, columns in ADDED_COLUMNS.items():
            existing = {
                row["name"] for row in
                conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# Columns added after the initial schema. Additive only — SQLite cannot
# drop or retype a column without rebuilding the table.
ADDED_COLUMNS = {
    "recipes": {
        "allergens": "TEXT NOT NULL DEFAULT '[]'",
        "tools": "TEXT NOT NULL DEFAULT '[]'",
        # NULL = not yet judged, 1 = yes, 0 = no. Kept three-state so an
        # unreviewed recipe isn't silently treated as unsuitable.
        "baby_friendly": "INTEGER",
        # What to change to make it suit the baby (baby_friendly = 2).
        "baby_note": "TEXT",
    },
    "weeks": {
        # When the order is delivered (ISO date) and an optional label.
        "delivery_date": "TEXT",
        "name": "TEXT",
    },
    "products": {
        # A stand-in rather than the real thing (e.g. butternut squash for
        # delica pumpkin). Shown as a "swap" tag on the Ingredients page.
        "substitute": "INTEGER NOT NULL DEFAULT 0",
        "note": "TEXT",
    },
    "ingredients": {
        # Things you always have. Never appear on a shopping list at all —
        # olive oil, salt, sugar, honey. Distinct from `cupboard`, which is
        # bought but kept in the cupboard.
        "assume_stocked": "INTEGER NOT NULL DEFAULT 0",
        # Carb type(s) for the carb filter. NULL = guess from the name,
        # '' = not a carb, else comma-separated keys (see filters.py).
        "carb": "TEXT",
    },
}


@contextmanager
def transaction():
    """One connection for a group of writes that must all land or none.

    Each execute() call opens its own connection and commits, so a failure
    part-way through a multi-row insert leaves a half-written record. Recipe
    ingest writes a recipe, its ingredients and its steps — that has to be
    atomic.
    """
    with connect() as conn:
        yield conn


def query(sql, params=()):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def new_week_id():
    """Unguessable id — week URLs are public."""
    return secrets.token_urlsafe(9)


def cupboard_balance(ingredient_id):
    """Derived from the ledger, never stored.

    observed_empty resets the running total to zero at that date;
    observed_sufficient raises it to the floor observed.
    """
    rows = query(
        "SELECT change, source FROM cupboard_ledger "
        "WHERE ingredient_id = ? ORDER BY event_date, id",
        (ingredient_id,),
    )
    balance = 0.0
    for row in rows:
        if row["source"] == "observed_empty":
            balance = 0.0
        elif row["source"] == "observed_sufficient":
            balance = max(balance, row["change"])
        else:
            balance += row["change"]
    return max(balance, 0.0)
