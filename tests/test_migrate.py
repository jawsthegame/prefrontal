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


def test_coaching_state_default_backfill_reaches_existing_users(tmp_path):
    """A default key added after provisioning is back-filled to existing users.

    Simulates the real deployment: a user was provisioned before `home_zip`
    existed (so the key is missing), then a later `init_db` runs the migration
    ladder and seeds the absent default — without clobbering values already set.
    """
    from prefrontal.clarify import HOME_ZIP_KEY, LOCALIZATION_KEY
    from prefrontal.memory.db import init_db
    from prefrontal.memory.migrate import backfill_coaching_state_defaults
    from prefrontal.memory.store import MemoryStore, provision_user

    db = tmp_path / "prefrontal.db"
    conn = init_db(str(db))
    store = MemoryStore(conn)
    user, _ = provision_user(store, "tom", display_name="Tom", is_operator=True)
    scoped = store.scoped(user["id"])
    # Simulate the pre-existing user: drop the newer keys and set a value that
    # must survive the back-fill (never clobbered).
    conn.execute(
        "DELETE FROM coaching_state WHERE user_id = ? AND key IN (?, ?)",
        (user["id"], HOME_ZIP_KEY, LOCALIZATION_KEY),
    )
    scoped.set_state("preferred_briefing_format", "long", source="explicit")
    conn.commit()
    assert scoped.get_state(HOME_ZIP_KEY) is None

    backfill_coaching_state_defaults(conn)

    assert scoped.get_state(HOME_ZIP_KEY) == "19027"
    assert scoped.get_state(LOCALIZATION_KEY) == "0"  # opt-in: seeded off
    # A value the user set is left untouched.
    assert scoped.get_state("preferred_briefing_format") == "long"
    conn.close()


def test_coaching_state_backfill_noop_on_empty_db(tmp_path):
    """With no users yet, the coaching-state back-fill does nothing (and doesn't raise)."""
    from prefrontal.memory.db import init_db
    from prefrontal.memory.migrate import backfill_coaching_state_defaults

    db = tmp_path / "empty.db"
    conn = init_db(str(db))
    backfill_coaching_state_defaults(conn)  # no users → no-op
    n = conn.execute("SELECT COUNT(*) FROM coaching_state").fetchone()[0]
    assert n == 0
    conn.close()
