"""Tests for triage.apply — routing a decision into the store, nudging, idempotency."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.triage import Signal, TriageDecision, apply

TODAY = date(2026, 7, 3)


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "a", token="a-tok")
    try:
        yield s.scoped(s.get_user("a")["id"])
    finally:
        conn.close()


class FakeN8n:
    def __init__(self, delivered=True):
        self.calls = []
        self._delivered = delivered

    def trigger(self, event, payload=None):
        self.calls.append((event, payload))
        return SimpleNamespace(delivered=self._delivered)


def _decide(kind, urgency, route, *, when=None):
    fields = {"when": when} if when else {}
    return TriageDecision(
        kind=kind, urgency=urgency, route=route, reason="because",
        confidence=0.8, source="heuristic", fields=fields,
    )


def _sig(**over):
    base = dict(source="mail", title="A thing", external_id="")
    base.update(over)
    return Signal(**base)


# --- each route creates exactly the right row --------------------------------


def test_todo_route_creates_an_augmented_todo(store):
    rep = apply(
        _sig(title="Pay the invoice"), _decide("action", "today", "todo"), store, today=TODAY
    )
    assert rep["routed_ref"].startswith("todo:")
    todos = store.open_todos()
    assert len(todos) == 1 and todos[0]["title"] == "Pay the invoice"
    assert todos[0]["source"] == "triage"
    # And it's in the audit log tied to the created row.
    log = store.recent_triage()
    assert len(log) == 1 and log[0]["routed_ref"] == rep["routed_ref"]


def test_commitment_route_with_a_date_creates_a_commitment(store):
    # Clock-relative: upcoming_commitments filters against the real clock, so the
    # target date must be in the future relative to *now*, not a fixed calendar day.
    today = date.today()
    when = (today + timedelta(days=4)).isoformat()
    rep = apply(
        _sig(title="Dentist Tuesday"),
        _decide("commitment", "later", "commitment", when=when),
        store, today=today,
    )
    assert rep["routed_ref"].startswith("commitment:")
    ups = store.upcoming_commitments()
    assert len(ups) == 1 and ups[0]["title"] == "Dentist Tuesday"


def test_commitment_without_a_date_is_logged_but_not_routed(store):
    rep = apply(
        _sig(title="Some meeting"), _decide("commitment", "later", "commitment"),
        store, today=TODAY,
    )
    assert rep["routed_ref"] is None
    assert store.upcoming_commitments() == []
    assert store.recent_triage()[0]["route"] == "commitment"  # classification still logged


def test_commitment_with_an_unparseable_date_is_logged_but_not_routed(store):
    """A garbage `when` (to_utc raises) drops to logged-only, never a bad commitment."""
    rep = apply(
        _sig(title="Some meeting"),
        _decide("commitment", "later", "commitment", when="not-a-date"),
        store, today=TODAY,
    )
    assert rep["routed_ref"] is None
    assert store.upcoming_commitments() == []
    assert store.recent_triage()[0]["route"] == "commitment"  # still logged


def test_episode_route_logs_an_episode(store):
    rep = apply(
        _sig(title="I made it on time"), _decide("outcome", "none", "episode"),
        store, today=TODAY,
    )
    assert rep["routed_ref"].startswith("episode:")
    assert len(store.all_episodes()) == 1


def test_surface_and_drop_and_state_write_nothing_to_core(store):
    for route in ("surface", "drop", "state"):
        rep = apply(_sig(title=route), _decide("info", "none", route), store, today=TODAY)
        assert rep["routed_ref"] is None
    assert store.open_todos() == [] and store.all_episodes() == []
    assert len(store.recent_triage()) == 3  # all three still logged


# --- urgent nudge ------------------------------------------------------------


def test_urgency_now_fires_one_triage_urgent_event(store):
    n8n = FakeN8n()
    rep = apply(
        _sig(title="OVERDUE"), _decide("action", "now", "todo"), store, n8n=n8n, today=TODAY
    )
    assert rep["fired"] is True
    assert len(n8n.calls) == 1 and n8n.calls[0][0] == "triage.urgent"
    assert n8n.calls[0][1]["routed_ref"] == rep["routed_ref"]


def test_non_urgent_does_not_fire(store):
    n8n = FakeN8n()
    apply(_sig(), _decide("action", "today", "todo"), store, n8n=n8n, today=TODAY)
    assert n8n.calls == []


def test_absent_n8n_is_a_no_op(store):
    rep = apply(_sig(), _decide("action", "now", "todo"), store, n8n=None, today=TODAY)
    assert rep["fired"] is False  # nothing to fire, no error


def test_urgent_but_n8n_not_delivered_reports_not_fired(store):
    """fired mirrors the n8n result: the event is sent, but delivered=False → fired=False."""
    n8n = FakeN8n(delivered=False)
    rep = apply(
        _sig(title="OVERDUE"), _decide("action", "now", "todo"), store, n8n=n8n, today=TODAY
    )
    assert len(n8n.calls) == 1     # the urgent event was attempted...
    assert rep["fired"] is False   # ...but n8n reported it wasn't delivered


# --- source-supplied routing (the mail-unification seam) --------------------


def test_todo_route_uses_a_prebuilt_payload_verbatim(store):
    """A source with its own classifier (mail) can hand the router a fully-built
    todo in fields["todo"]; it's created as-is, not re-augmented."""
    d = TriageDecision(
        kind="action", urgency="today", route="todo", reason="mail: reply",
        confidence=0.6, source="heuristic",
        fields={"todo": {
            "title": "Reply to Sam: contract", "notes": "[mail/work] from sam",
            "priority": 3, "domain": "work", "source": "manual",
        }},
    )
    rep = apply(_sig(title="Contract?"), d, store, today=TODAY)
    (todo,) = store.open_todos()
    assert rep["routed_ref"] == f"todo:{todo['id']}"
    assert todo["title"] == "Reply to Sam: contract"
    assert todo["notes"] == "[mail/work] from sam"
    assert todo["priority"] == 3 and todo["domain"] == "work"
    assert todo["source"] == "manual"  # provenance preserved, not overwritten to "triage"


def test_apply_honors_a_prebuilt_routed_ref_without_re_routing(store):
    """When the caller already linked the row (mail closing a delegation loop), the
    supplied routed_ref is logged and no new row is created."""
    rep = apply(
        _sig(title="Re: the deck", external_id="mail:1"),
        TriageDecision(
            kind="action", urgency="today", route="todo",
            reason="mail: reply · delegation returned", confidence=0.9,
            source="llm", fields={"routed_ref": "todo:99"},
        ),
        store,
        today=TODAY,
    )
    assert rep["routed_ref"] == "todo:99"
    assert store.open_todos() == []  # nothing created — the todo already existed
    (row,) = store.recent_triage()
    assert row["routed_ref"] == "todo:99" and row["route"] == "todo"
    assert "delegation returned" in row["reason"]


# --- idempotency -------------------------------------------------------------


def test_redelivery_is_idempotent(store):
    d = _decide("action", "today", "todo")
    first = apply(_sig(external_id="gmail-1"), d, store, today=TODAY)
    second = apply(_sig(external_id="gmail-1"), d, store, today=TODAY)
    assert second["idempotent"] is True
    assert second["triage_id"] == first["triage_id"]
    assert second["routed_ref"] == first["routed_ref"]
    # Only one todo and one log row despite two applies.
    assert len(store.open_todos()) == 1
    assert len(store.recent_triage()) == 1
