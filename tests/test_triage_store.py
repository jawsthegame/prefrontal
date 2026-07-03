"""Tests for the triage_log store methods — logging, recency, idempotency, scope."""

from __future__ import annotations

import sqlite3

import pytest

from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "a", token="a-tok")
    provision_user(s, "b", token="b-tok")
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def a(store):
    return store.scoped(store.get_user("a")["id"])


@pytest.fixture()
def b(store):
    return store.scoped(store.get_user("b")["id"])


def _log(scoped, **over):
    base = dict(
        source="mail", title="Overdue invoice", kind="action", urgency="now",
        route="todo", reason="actionable request", confidence=0.85,
        decided_by="heuristic",
    )
    base.update(over)
    return scoped.log_triage(**base)


def test_log_and_recent_round_trip(a):
    tid = _log(a, routed_ref="todo:7")
    rows = a.recent_triage()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == tid
    assert (r["kind"], r["urgency"], r["route"]) == ("action", "now", "todo")
    assert r["reason"] == "actionable request"
    assert r["decided_by"] == "heuristic"
    assert r["routed_ref"] == "todo:7"
    assert r["received_at"]  # stamped even though we didn't pass one


def test_recent_is_newest_first_and_limited(a):
    for i in range(5):
        _log(a, title=f"item {i}", received_at=f"2026-07-0{i + 1} 09:00:00")
    rows = a.recent_triage(limit=3)
    assert [r["title"] for r in rows] == ["item 4", "item 3", "item 2"]


def test_triage_seen_finds_prior_decision_by_external_id(a):
    _log(a, external_id="gmail-123", routed_ref="todo:1")
    seen = a.triage_seen("mail", "gmail-123")
    assert seen is not None and seen["routed_ref"] == "todo:1"
    # A different id, or a blank id, is not a hit.
    assert a.triage_seen("mail", "gmail-999") is None
    assert a.triage_seen("mail", "") is None


def test_blank_external_id_is_never_deduped(a):
    # Two blank-id rows both persist (the partial unique index excludes '').
    _log(a, external_id="")
    _log(a, external_id="")
    assert len(a.recent_triage()) == 2


def test_duplicate_external_id_is_rejected_by_the_index(a):
    _log(a, external_id="gmail-dup")
    with pytest.raises(sqlite3.IntegrityError):  # the partial unique index
        _log(a, external_id="gmail-dup")


def test_triage_log_is_scoped_per_user(a, b):
    _log(a, title="a's signal", external_id="shared-id")
    # Same external_id is fine for a different user (index is per-user).
    _log(b, title="b's signal", external_id="shared-id")
    assert [r["title"] for r in a.recent_triage()] == ["a's signal"]
    assert [r["title"] for r in b.recent_triage()] == ["b's signal"]
    assert a.triage_seen("mail", "shared-id")["title"] == "a's signal"
