"""Tests for blockers — the "who's waiting on you" tracker.

Covers the pure helpers (aging, description, the digest line), the store repo
(CRUD, ordering, resolve/reopen lifecycle), the HTTP router, the panic-mode and
morning-briefing surfacing that makes a blocker weigh into prioritization, and
the NL-assistant ``add_blocker`` capture op.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.blockers import (
    blocker_summary_line,
    describe_blocker,
    normalize_person,
    waited_phrase,
    waiting_days,
)
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from tests.conftest import scoped_default

SECRET = "blockers-secret"
NOW = "2026-07-14 12:00:00"


def _now():
    from prefrontal.clock import parse_ts

    return parse_ts(NOW)


def _offline_ollama() -> OllamaClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


# --- pure helpers ----------------------------------------------------------


def test_normalize_person_collapses_whitespace():
    assert normalize_person("  Sam   Smith \n") == "Sam Smith"
    assert normalize_person("") == ""


def test_waiting_days_floors_at_zero():
    now = _now()
    assert waiting_days("2026-07-09 12:00:00", now) == 5
    assert waiting_days("2026-07-14 09:00:00", now) == 0  # earlier today
    assert waiting_days("2026-07-20 09:00:00", now) == 0  # future, not negative
    assert waiting_days(None, now) == 0  # unparseable


def test_waited_phrase():
    now = _now()
    assert waited_phrase("2026-07-14 08:00:00", now) == "today"
    assert waited_phrase("2026-07-11 08:00:00", now) == "3d"


def test_describe_blocker():
    now = _now()
    line = describe_blocker(
        {"person": "Sam", "what": "the budget numbers", "blocking_since": "2026-07-11 09:00:00"},
        now,
    )
    assert line == "Sam — the budget numbers (waiting 3d)"


def test_summary_line_none_when_empty():
    assert blocker_summary_line([], _now()) is None


def test_summary_line_names_longest_waiter():
    now = _now()
    rows = [
        {"person": "Sam", "blocking_since": "2026-07-13 09:00:00"},
        {"person": "Priya", "blocking_since": "2026-07-04 09:00:00"},
    ]
    line = blocker_summary_line(rows, now)
    assert "2 people are waiting on you" in line
    assert "Priya (10d)" in line


def test_summary_line_singular():
    now = _now()
    line = blocker_summary_line([{"person": "Sam", "blocking_since": "2026-07-13 09:00:00"}], now)
    assert line.startswith("1 person is waiting on you")


# --- store repo ------------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_add_and_get(store):
    bid = store.add_blocker("Sam", "the numbers", priority=2, notes="Q3")
    b = store.get_blocker(bid)
    assert b["person"] == "Sam" and b["what"] == "the numbers"
    assert b["priority"] == 2 and b["notes"] == "Q3"
    assert b["status"] == "open" and b["resolved_at"] is None


def test_get_unknown_is_none(store):
    assert store.get_blocker(424242) is None


def test_add_with_explicit_blocking_since(store):
    bid = store.add_blocker("Sam", "x", blocking_since="2026-06-01 09:00:00")
    assert store.get_blocker(bid)["blocking_since"] == "2026-06-01 09:00:00"


def test_list_orders_by_priority_then_wait(store):
    a = store.add_blocker("A", "low, old", priority=0, blocking_since="2026-01-01 09:00:00")
    b = store.add_blocker("B", "urgent", priority=3, blocking_since="2026-07-01 09:00:00")
    c = store.add_blocker("C", "urgent, older", priority=3, blocking_since="2026-06-01 09:00:00")
    ids = [row["id"] for row in store.open_blockers()]
    # urgent before low; within urgent, longest wait (older blocking_since) first.
    assert ids == [c, b, a]


def test_count_open(store):
    store.add_blocker("A", "x")
    store.add_blocker("B", "y")
    assert store.count_open_blockers() == 2


def test_resolve_and_reopen_lifecycle(store):
    bid = store.add_blocker("Sam", "x")
    assert store.count_open_blockers() == 1
    resolved = store.resolve_blocker(bid)
    assert resolved["status"] == "resolved" and resolved["resolved_at"] is not None
    assert store.count_open_blockers() == 0
    # Resolved rows are excluded from open list but kept in include_resolved.
    assert store.open_blockers() == []
    assert len(store.list_blockers(include_resolved=True)) == 1
    reopened = store.reopen_blocker(bid)
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    assert store.count_open_blockers() == 1


def test_resolve_unknown_is_none(store):
    assert store.resolve_blocker(999) is None


def test_reopen_unknown_is_none(store):
    assert store.reopen_blocker(999) is None


def test_update_fields(store):
    bid = store.add_blocker("Sam", "x", priority=1)
    updated = store.update_blocker(bid, priority=3, what="the Q3 numbers", person="Samuel")
    assert updated["priority"] == 3
    assert updated["what"] == "the Q3 numbers"
    assert updated["person"] == "Samuel"


def test_update_ignores_unknown_columns(store):
    bid = store.add_blocker("Sam", "x")
    # status is not an allowed field here (use resolve/reopen); it's silently dropped.
    updated = store.update_blocker(bid, status="resolved", nonsense=1)
    assert updated["status"] == "open"


def test_update_unknown_blocker_is_none(store):
    assert store.update_blocker(999, priority=2) is None


def test_blockers_are_user_scoped(store):
    # A second user's blockers never appear in the first user's list.
    store.add_blocker("Sam", "mine")
    other = scoped_default(MemoryStore(store.conn), handle="other")
    other.add_blocker("Bob", "theirs")
    assert [b["person"] for b in store.open_blockers()] == ["Sam"]
    assert [b["person"] for b in other.open_blockers()] == ["Bob"]


# --- HTTP API --------------------------------------------------------------


@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store_open):
    from prefrontal.webhooks.app import create_app

    app = create_app(
        store=store_open, settings=Settings(webhook_secret=SECRET), ollama=_offline_ollama()
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_api_create_list_get(client):
    r = client.post(
        "/blockers",
        json={"person": "Sam", "what": "the budget numbers", "priority": 2},
        headers=_auth(),
    )
    assert r.status_code == 201
    bid = r.json()["id"]
    listed = client.get("/blockers", headers=_auth()).json()["blockers"]
    assert listed[0]["id"] == bid and listed[0]["person"] == "Sam"
    got = client.get(f"/blockers/{bid}", headers=_auth()).json()
    assert got["what"] == "the budget numbers"


def test_api_create_requires_person_and_what(client):
    assert client.post(
        "/blockers", json={"person": "  ", "what": "x"}, headers=_auth()
    ).status_code == 422
    assert client.post(
        "/blockers", json={"person": "Sam", "what": "  "}, headers=_auth()
    ).status_code == 422


def test_api_get_unknown_404(client):
    assert client.get("/blockers/999", headers=_auth()).status_code == 404


def test_api_patch(client):
    bid = client.post(
        "/blockers", json={"person": "Sam", "what": "x"}, headers=_auth()
    ).json()["id"]
    r = client.patch(f"/blockers/{bid}", json={"priority": 3}, headers=_auth())
    assert r.status_code == 200 and r.json()["priority"] == 3


def test_api_patch_blank_person_rejected(client):
    bid = client.post(
        "/blockers", json={"person": "Sam", "what": "x"}, headers=_auth()
    ).json()["id"]
    assert client.patch(
        f"/blockers/{bid}", json={"person": " "}, headers=_auth()
    ).status_code == 422


def test_api_patch_unknown_404(client):
    assert client.patch("/blockers/999", json={"priority": 2}, headers=_auth()).status_code == 404


def test_api_resolve_and_reopen(client):
    bid = client.post(
        "/blockers", json={"person": "Sam", "what": "x"}, headers=_auth()
    ).json()["id"]
    assert client.post(f"/blockers/{bid}/resolve", headers=_auth()).json()["status"] == "resolved"
    # Resolved drops out of the default (open-only) listing.
    assert client.get("/blockers", headers=_auth()).json()["blockers"] == []
    assert (
        len(client.get("/blockers?include_resolved=true", headers=_auth()).json()["blockers"]) == 1
    )
    assert client.post(f"/blockers/{bid}/reopen", headers=_auth()).json()["status"] == "open"


def test_api_resolve_unknown_404(client):
    assert client.post("/blockers/999/resolve", headers=_auth()).status_code == 404


# --- panic-mode surfacing --------------------------------------------------


def test_panic_buckets_blockers(store):
    from prefrontal.panic import build_panic

    now = _now()
    store.add_blocker("Sam", "smoulder", priority=1, blocking_since="2026-07-09 09:00:00")
    store.add_blocker("Priya", "urgent thing", priority=3, blocking_since="2026-07-13 09:00:00")
    store.add_blocker(
        "Dana",
        "overdue thing",
        deadline="2026-07-10 17:00:00",
        blocking_since="2026-07-08 09:00:00",
    )
    plan = build_panic(store, now=now)
    late = [p for p in plan.late if p.kind == "blocker"]
    soon = [p for p in plan.soon if p.kind == "blocker"]
    piling = [p for p in plan.piling_up if p.kind == "blocker"]
    assert len(late) == 1 and "Dana" in late[0].title
    assert len(soon) == 1 and "Priya" in soon[0].title
    assert len(piling) == 1 and "Sam" in piling[0].title
    # The blocker-specific first step is chosen when a blocker is the top pressure.
    assert plan.first_step and "update" in plan.first_step.lower()


def test_panic_blocker_outranks_own_smoulder(store):
    from prefrontal.panic import build_panic

    now = _now()
    # An avoided todo (piling up) vs a blocker (piling up): the person waiting wins.
    store.add_todo("some avoided thing", priority=1)
    store.add_blocker("Sam", "waiting a while", blocking_since="2026-07-01 09:00:00")
    plan = build_panic(store, now=now)
    assert plan.piling_up  # both may show, but the blocker sorts first
    assert plan.piling_up[0].kind == "blocker"


def test_panic_no_blockers_is_silent(store):
    from prefrontal.panic import build_panic

    plan = build_panic(store, now=_now())
    assert all(p.kind != "blocker" for p in plan.late + plan.soon + plan.piling_up)


def test_panic_preserves_priority_zero(store):
    # A valid priority 0 (low) must not be silently upgraded to 1, which would
    # score it identically to a genuine priority-1 blocker.
    from prefrontal.panic import build_panic

    now = _now()
    store.add_blocker("Lo", "low thing", priority=0, blocking_since="2026-07-10 09:00:00")
    store.add_blocker("Hi", "normal thing", priority=1, blocking_since="2026-07-10 09:00:00")
    piling = {p.title.split()[0]: p for p in build_panic(store, now=now).piling_up}
    assert piling["Lo"].score < piling["Hi"].score


# --- briefing surfacing ----------------------------------------------------


def test_briefing_surfaces_blockers(store):
    from prefrontal.briefing import build_briefing, render_briefing

    now = _now()
    store.add_blocker("Sam", "the numbers", blocking_since="2026-07-09 09:00:00")
    briefing = build_briefing(store, now=now)
    assert briefing.blocked and briefing.blocked[0]["person"] == "Sam"
    assert briefing.blocked[0]["waiting_days"] == 5
    rendered = render_briefing(briefing)
    assert "Waiting on you" in rendered
    assert "Sam — the numbers (waiting 5d)" in rendered


def test_briefing_blocker_waiting_today(store):
    # A blocker logged today (0 days) still shows timing — "today", not blank.
    from prefrontal.briefing import build_briefing, render_briefing

    now = _now()
    store.add_blocker("Sam", "the numbers", blocking_since="2026-07-14 09:00:00")
    briefing = build_briefing(store, now=now)
    assert briefing.blocked[0]["waiting_days"] == 0
    assert "Sam — the numbers (waiting today)" in render_briefing(briefing)


def test_briefing_no_blockers_no_section(store):
    from prefrontal.briefing import build_briefing, render_briefing

    briefing = build_briefing(store, now=_now())
    assert briefing.blocked == []
    assert "Waiting on you" not in render_briefing(briefing)


# --- NL assistant capture --------------------------------------------------


def test_assistant_add_blocker_op(store):
    from prefrontal.assistant import ALLOWED_OPS, _execute_one, _validate_one

    assert "add_blocker" in ALLOWED_OPS
    action = _validate_one(
        {"op": "add_blocker", "person": " Sam ", "what": "the Q3 numbers", "priority": 2}, {}
    )
    assert action.params["person"] == "Sam"
    result = _execute_one(store, action, "UTC")
    assert result["ok"] and result["detail"].startswith("blocker #")
    assert store.open_blockers()[0]["what"] == "the Q3 numbers"


def test_assistant_add_blocker_requires_person(store):
    from prefrontal.assistant import _ActionError, _validate_one

    with pytest.raises(_ActionError):
        _validate_one({"op": "add_blocker", "what": "x"}, {})
