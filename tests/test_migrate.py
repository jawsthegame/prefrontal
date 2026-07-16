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
    backfill_project_ranks,
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


def test_backfill_adds_away_behavior_default_to_existing_chores():
    """A pre-away_behavior chores row gains the column with its 'keep' default."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # An "old" household_chores table with every original column *except*
    # away_behavior (which schema.sql later grew). Keeping the CURRENT_TIMESTAMP
    # columns present means the only column the back-fill adds is away_behavior —
    # which must carry a constant default so ADD COLUMN is legal.
    conn.execute(
        "CREATE TABLE household_chores ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, household_id INTEGER NOT NULL, "
        "title TEXT NOT NULL, owner_id INTEGER, routine_id INTEGER, "
        "days TEXT NOT NULL DEFAULT '', month_days TEXT NOT NULL DEFAULT '', "
        "due_time TEXT NOT NULL DEFAULT '', remind_before INTEGER NOT NULL DEFAULT 30, "
        "impact TEXT, enabled INTEGER NOT NULL DEFAULT 1, updated_by INTEGER, "
        "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "last_reminded_on TEXT, last_missed_on TEXT)"
    )
    conn.execute(
        "INSERT INTO household_chores (household_id, title) VALUES (1, 'trash')"
    )
    assert "away_behavior" not in _columns(conn, "household_chores")

    backfill_added_columns(conn)

    assert "away_behavior" in _columns(conn, "household_chores")
    # The constant DEFAULT reaches the pre-existing row (not NULL).
    row = conn.execute("SELECT away_behavior FROM household_chores WHERE title='trash'").fetchone()
    assert row["away_behavior"] == "keep"


def test_project_rank_backfill_assigns_contiguous_ranks_per_user():
    """A pre-rank projects table gets contiguous ranks per user (active only)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # A pre-`rank` projects table (original columns only).
    conn.execute(
        "CREATE TABLE projects ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
        "name TEXT NOT NULL, description TEXT, domain TEXT NOT NULL, notes TEXT, "
        "color TEXT, status TEXT NOT NULL DEFAULT 'active', "
        "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.executemany(
        "INSERT INTO projects (user_id, name, domain, status) VALUES (?, ?, 'home', ?)",
        [(1, "u1-a", "active"), (1, "u1-b", "active"),
         (1, "u1-archived", "archived"), (2, "u2-a", "active")],
    )

    backfill_added_columns(conn)  # adds the NULL `rank` column
    backfill_project_ranks(conn)  # then fills it

    rows = {r["name"]: r["rank"] for r in conn.execute("SELECT name, rank FROM projects")}
    assert rows["u1-a"] == 1 and rows["u1-b"] == 2  # per-user, by id
    assert rows["u1-archived"] is None             # archived stays unranked
    assert rows["u2-a"] == 1                        # separate user restarts at 1

    # Idempotent + only touches NULLs: a manual reorder survives a second run.
    conn.execute("UPDATE projects SET rank = 2 WHERE name = 'u1-a'")
    conn.execute("UPDATE projects SET rank = 1 WHERE name = 'u1-b'")
    backfill_project_ranks(conn)
    rows = {r["name"]: r["rank"] for r in conn.execute("SELECT name, rank FROM projects")}
    assert rows["u1-a"] == 2 and rows["u1-b"] == 1


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


def test_responsive_hours_seed_default_is_22():
    """The seed must match coaching.DEFAULT_RESPONSIVE_END, not the old 14:00 bug."""
    from prefrontal.coaching import DEFAULT_RESPONSIVE_END
    from prefrontal.memory._helpers import DEFAULT_COACHING_STATE

    seed = dict((k, v) for k, v, _ in DEFAULT_COACHING_STATE)
    assert seed["responsive_hours_end"] == "22:00"
    assert seed["responsive_hours_end"] == f"{DEFAULT_RESPONSIVE_END:02d}:00"


def test_responsive_hours_seed_reset_fixes_inferred_not_explicit(tmp_path):
    """The one-time reset moves the buggy seeded 14:00 to 22:00, but leaves a value
    the user deliberately chose (source ``explicit``) untouched, and is idempotent."""
    from prefrontal.memory.db import init_db
    from prefrontal.memory.migrate import reset_seeded_responsive_hours_end
    from prefrontal.memory.store import MemoryStore, provision_user

    db = tmp_path / "prefrontal.db"
    conn = init_db(str(db))
    store = MemoryStore(conn)
    old, _ = provision_user(store, "old", display_name="Old", is_operator=True)
    keep, _ = provision_user(store, "keep", display_name="Keep")
    old_s = store.scoped(old["id"])
    keep_s = store.scoped(keep["id"])
    # One user still carries the old buggy seed; another deliberately set 14:00.
    old_s.set_state("responsive_hours_end", "14:00", source="inferred")
    keep_s.set_state("responsive_hours_end", "14:00", source="explicit")
    conn.commit()

    reset_seeded_responsive_hours_end(conn)

    assert old_s.get_state("responsive_hours_end") == "22:00"   # buggy seed corrected
    assert keep_s.get_state("responsive_hours_end") == "14:00"  # explicit choice kept
    # Idempotent: nothing left matching 14:00/inferred → a re-run is a no-op.
    reset_seeded_responsive_hours_end(conn)
    assert old_s.get_state("responsive_hours_end") == "22:00"
    conn.close()


def test_responsive_hours_seed_reset_noop_on_empty_db(tmp_path):
    """No users yet → the reset does nothing and doesn't raise."""
    from prefrontal.memory.db import init_db
    from prefrontal.memory.migrate import reset_seeded_responsive_hours_end

    db = tmp_path / "empty.db"
    conn = init_db(str(db))
    reset_seeded_responsive_hours_end(conn)  # no rows → no-op
    conn.close()
