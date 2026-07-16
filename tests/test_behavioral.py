"""The queryable behavioral model — entity-scoped continuity for one todo.

Covers the ``todo_events`` change log written by ``update_todo_deadline`` /
``defer_todo``, the read-side model in :mod:`prefrontal.memory.behavioral`
(counts, recency, estimate bias, and the second-person context lines an agent
retrieves), the ``GET /todos/{id}/behavior`` HTTP surface, and the
``profile --todo`` CLI path.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import utcnow
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.behavioral import (
    _ago_phrase,
    _count_phrase,
    todo_behavior,
)
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from tests.conftest import scoped_default

SECRET = "behavioral-secret"


@pytest.fixture()
def store():
    """A MemoryStore on a fresh in-memory, schema-initialized DB (default user)."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


# --- change-log recording (repo) -------------------------------------------


def test_first_deadline_is_not_a_reschedule(store):
    tid = store.add_todo("file taxes")
    assert store.update_todo_deadline(tid, "2026-04-15 12:00:00") is True
    # Establishing the first deadline is not a move — nothing logged.
    assert store.count_todo_events(tid, "rescheduled") == 0


def test_moving_a_deadline_logs_a_reschedule(store):
    tid = store.add_todo("file taxes")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store.update_todo_deadline(tid, "2026-04-20 12:00:00")
    store.update_todo_deadline(tid, "2026-04-25 12:00:00")
    assert store.count_todo_events(tid, "rescheduled") == 2
    events = store.todo_events(tid, event_type="rescheduled")
    # Oldest first; before/after captured.
    assert events[0]["old_value"] == "2026-04-15 12:00:00"
    assert events[0]["new_value"] == "2026-04-20 12:00:00"
    assert events[-1]["new_value"] == "2026-04-25 12:00:00"


def test_reschedule_to_same_value_is_not_logged(store):
    tid = store.add_todo("file taxes")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    assert store.count_todo_events(tid, "rescheduled") == 0


def test_clearing_an_existing_deadline_logs_a_reschedule(store):
    tid = store.add_todo("file taxes")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    assert store.update_todo_deadline(tid, None) is True
    events = store.todo_events(tid, event_type="rescheduled")
    assert len(events) == 1
    assert events[0]["old_value"] == "2026-04-15 12:00:00"
    assert events[0]["new_value"] is None


def test_deadline_move_on_closed_todo_logs_nothing(store):
    tid = store.add_todo("file taxes")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store.close_todo(tid, "done")
    # Closed todos aren't editable — the no-op writes no event.
    assert store.update_todo_deadline(tid, "2026-05-01 12:00:00") is False
    assert store.count_todo_events(tid, "rescheduled") == 0


def test_defer_logs_each_park_but_not_the_unpark(store):
    tid = store.add_todo("call dentist")
    store.defer_todo(tid, "2026-07-20 09:00:00")
    store.defer_todo(tid, "2026-07-25 09:00:00")
    assert store.count_todo_events(tid, "deferred") == 2
    # Un-deferring (clearing) is not itself a deferral.
    assert store.defer_todo(tid, None) is True
    assert store.count_todo_events(tid, "deferred") == 2


def test_events_are_scoped_per_user(store):
    tid = store.add_todo("shared-ish")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store.update_todo_deadline(tid, "2026-04-20 12:00:00")
    other = scoped_default(MemoryStore(store.conn), handle="other")
    # Another user reading the same id sees none of these events.
    assert other.todo_events(tid) == []


# --- behavioral model (read side) ------------------------------------------


def test_todo_behavior_unknown_returns_none(store):
    assert todo_behavior(store, 9999) is None


def test_todo_behavior_counts_and_context(store):
    tid = store.add_todo("submit reimbursement")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    for day in ("16", "17", "18", "19"):
        store.update_todo_deadline(tid, f"2026-04-{day} 12:00:00")
    # 4 moves after the initial set.
    behavior = todo_behavior(store, tid)
    assert behavior is not None
    assert behavior.reschedule_count == 4
    assert behavior.defer_count == 0
    # The flagship line, in natural prose.
    assert any("rescheduled this 4 times" in line for line in behavior.context_lines)
    # 3+ reschedules escalates to the "it keeps sliding" nudge.
    assert any("keeps sliding" in line for line in behavior.context_lines)


def test_todo_behavior_snooze_phrasing_and_parked_flag(store):
    tid = store.add_todo("renew passport")
    future = (utcnow() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    store.defer_todo(tid, future)
    store.defer_todo(tid, future)
    behavior = todo_behavior(store, tid)
    assert behavior.defer_count == 2
    assert behavior.currently_snoozed is True
    assert any("snoozed this twice" in line for line in behavior.context_lines)
    assert any("currently parked" in line for line in behavior.context_lines)


def test_todo_behavior_recency_and_age(store):
    tid = store.add_todo("dentist")
    store.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store.update_todo_deadline(tid, "2026-04-20 12:00:00")
    # Read from 20 days in the future so recency/age phrasing is deterministic.
    later = utcnow() + timedelta(days=20)
    behavior = todo_behavior(store, tid, now=later)
    assert behavior.last_rescheduled_ago == "3 weeks ago"
    assert behavior.days_open is not None and round(behavior.days_open) == 20
    assert any("open 20 days" in line for line in behavior.context_lines)


def test_todo_behavior_estimate_bias_line(store):
    tid = store.add_todo("clean garage", category="errands")
    store.set_state(
        "time_estimation_bias:category:errands", "1.4", source="inferred"
    )
    behavior = todo_behavior(store, tid)
    assert behavior.estimate_bias == pytest.approx(1.4)
    assert any(
        "underestimate errands tasks by ~40%" in line
        for line in behavior.context_lines
    )


def test_todo_behavior_calibrated_bias_is_not_surfaced(store):
    tid = store.add_todo("quick email", category="admin")
    store.set_state(
        "time_estimation_bias:category:admin", "1.02", source="inferred"
    )
    behavior = todo_behavior(store, tid)
    # A ~1.0 multiplier is no deviation — not worth a line.
    assert behavior.estimate_bias is None
    assert not any("estimate" in line.lower() for line in behavior.context_lines)


def test_fresh_todo_has_no_context_lines(store):
    tid = store.add_todo("brand new")
    behavior = todo_behavior(store, tid)
    assert behavior.context_lines == []
    assert behavior.context == ""


# --- prose helpers ---------------------------------------------------------


def test_count_phrase():
    assert _count_phrase(1) == "once"
    assert _count_phrase(2) == "twice"
    assert _count_phrase(4) == "4 times"


def test_ago_phrase_bands():
    now = utcnow()
    assert _ago_phrase(now, now) == "today"
    assert _ago_phrase(now - timedelta(days=1, hours=2), now) == "yesterday"
    assert _ago_phrase(now - timedelta(days=5), now) == "5 days ago"
    assert _ago_phrase(now - timedelta(days=21), now) == "3 weeks ago"


# --- HTTP surface ----------------------------------------------------------


@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _offline_ollama() -> OllamaClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


@pytest.fixture()
def client(store_open):
    from prefrontal.webhooks.app import create_app

    app = create_app(
        store=store_open,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_api_behavior_returns_facts_and_context(client, store_open):
    tid = store_open.add_todo("file taxes")
    store_open.update_todo_deadline(tid, "2026-04-15 12:00:00")
    store_open.update_todo_deadline(tid, "2026-04-20 12:00:00")
    r = client.get(f"/todos/{tid}/behavior", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["todo_id"] == tid
    assert body["reschedule_count"] == 1
    assert isinstance(body["context_lines"], list)
    assert any("rescheduled this once" in line for line in body["context_lines"])


def test_api_behavior_unknown_404(client):
    assert client.get("/todos/9999/behavior", headers=_auth()).status_code == 404
