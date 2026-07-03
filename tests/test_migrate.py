"""Tests for schema evolution — the schema-derived column back-fill (§4).

The back-fill no longer carries a hand-maintained ledger of "columns added
later"; it diffs a live table against the shape ``schema.sql`` declares. These
tests lock that contract: ``schema.sql`` is the single source of truth, a stale
table is brought up to it, and a fresh database needs no back-fill at all.
"""
from __future__ import annotations

import sqlite3

from prefrontal.memory.db import SCHEMA_PATH
from prefrontal.memory.migrate import (
    _reference_columns,
    backfill_added_columns,
)
from prefrontal.memory.store import MemoryStore


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _all_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def test_reference_columns_are_derived_from_schema():
    """Every reference column matches a fresh schema.sql database, table for table."""
    ref = _reference_columns()
    assert ref, "reference schema should not be empty"
    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    fresh.executescript(SCHEMA_PATH.read_text())
    for table in _all_tables(fresh):
        want = {name for name, _ in ref[table]}
        assert want == _columns(fresh, table), table


def test_backfill_brings_a_stale_table_up_to_schema():
    """A table missing later-added columns gains exactly the schema's columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # An "old" households table: original columns only, predating the check-in /
    # digest / balance columns that schema.sql later grew.
    conn.execute(
        "CREATE TABLE households ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    assert "checkin_enabled" not in _columns(conn, "households")

    backfill_added_columns(conn)

    want = {name for name, _ in _reference_columns()["households"]}
    assert _columns(conn, "households") == want
    backfill_added_columns(conn)  # idempotent — a second run adds nothing


def test_backfill_is_a_noop_on_a_fresh_database():
    """A database created from schema.sql already has every column."""
    with MemoryStore.open(":memory:") as store:
        conn = store.conn
        before = {t: _columns(conn, t) for t in _all_tables(conn)}
        backfill_added_columns(conn)
        after = {t: _columns(conn, t) for t in _all_tables(conn)}
        assert before == after
