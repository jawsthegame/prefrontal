"""Tests for the shared repository base (prefrontal.memory.repos._base, §5)."""
from __future__ import annotations

import pytest

from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user


@pytest.fixture()
def scoped():
    conn = init_db(":memory:")
    try:
        store = MemoryStore(conn)
        user, _ = provision_user(store, "me", is_operator=True)
        yield store.scoped(user["id"])
    finally:
        conn.close()


def test_upsert_returning_id_is_stable_across_the_conflict_path(scoped):
    """The id comes back correct whether the row is inserted or updated."""
    first = scoped.upsert_pattern("time_estimation", "morning", observed_value=1.2, sample_size=3)
    # Second call hits ON CONFLICT DO UPDATE — lastrowid is unreliable there, so
    # the base reads the id back; it must match the original row.
    second = scoped.upsert_pattern("time_estimation", "morning", observed_value=1.5, sample_size=9)
    assert first == second
    # And the update actually took effect (same row, new values).
    row = next(p for p in scoped.get_patterns("time_estimation") if p["context_key"] == "morning")
    assert row["sample_size"] == 9


def test_query_one_returns_none_when_absent(scoped):
    """The _query_one helper maps a missing row to None (not an empty dict)."""
    assert scoped.get_geocode_cache("never-looked-up") is None
