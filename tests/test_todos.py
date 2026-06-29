"""Tests for todos (open loops) and time-fitting.

Covers the todo store, the pure free-window / fitting functions, the endpoints,
and the morning-briefing "spare time" tie-in.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from prefrontal.briefing import build_briefing
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import FreeWindow, fit_todos, free_windows, suggest_for_windows

SECRET = "todo-secret"


def _at(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# -- store -------------------------------------------------------------------


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield s


def test_todo_lifecycle(store):
    """Add → list (priority order) → done removes from open."""
    store.add_todo("Call dentist", estimate_minutes=10, priority=2)
    store.add_todo("Plan birthday", estimate_minutes=40, priority=1)
    open_ = store.open_todos()
    assert [t["title"] for t in open_] == ["Call dentist", "Plan birthday"]  # P2 first

    tid = open_[0]["id"]
    assert store.close_todo(tid, status="done") is True
    assert [t["title"] for t in store.open_todos()] == ["Plan birthday"]
    assert store.close_todo(tid) is False  # already closed
    assert store.get_todo(tid)["status"] == "done"


# -- fitting -----------------------------------------------------------------


def test_fit_todos_respects_bias_and_ranks():
    """A todo fits only if estimate*bias <= available; ranked by deadline/priority."""
    todos = [
        {"id": 1, "title": "Quick call", "estimate_minutes": 10, "priority": 1, "deadline": None},
        {"id": 2, "title": "Deadline soon", "estimate_minutes": 12, "priority": 1,
         "deadline": "2026-06-29 17:00:00"},
        {"id": 3, "title": "Too long", "estimate_minutes": 40, "priority": 3, "deadline": None},
        {"id": 4, "title": "No estimate", "priority": 3, "deadline": None},
    ]
    fits = fit_todos(20, todos, bias=1.4)  # 20 min free; bias 1.4
    titles = [f["todo"]["title"] for f in fits]
    # "Too long" (40*1.4) excluded; "No estimate" excluded; deadline ranks first.
    assert titles == ["Deadline soon", "Quick call"]
    assert fits[0]["effective_minutes"] == pytest.approx(16.8)


def test_free_windows_carves_gaps():
    """Gaps between commitments within the band are returned; short ones dropped."""
    day = datetime(2026, 6, 29, 9, 0, 0)
    commitments = [
        {"start_at": _at(day.replace(hour=10)), "end_at": _at(day.replace(hour=11))},
        {"start_at": _at(day.replace(hour=11, minute=5)), "end_at": _at(day.replace(hour=12))},
    ]
    windows = free_windows(
        commitments, day, day.replace(hour=13), min_minutes=10
    )
    # 9–10 (60m) and 12–13 (60m); the 5-min 11:00–11:05 gap is dropped.
    spans = [(w.start[11:16], round(w.minutes)) for w in windows]
    assert ("09:00", 60) in spans
    assert ("12:00", 60) in spans
    assert all(w.minutes >= 10 for w in windows)


def test_suggest_for_windows_no_double_booking():
    """Each window gets a distinct fitting todo."""
    windows = [FreeWindow("2026-06-29 09:00:00", "2026-06-29 09:20:00", 20),
               FreeWindow("2026-06-29 13:00:00", "2026-06-29 14:00:00", 60)]
    todos = [
        {"id": 1, "title": "Short", "estimate_minutes": 10, "priority": 2, "deadline": None},
        {"id": 2, "title": "Long", "estimate_minutes": 45, "priority": 1, "deadline": None},
    ]
    out = suggest_for_windows(windows, todos, bias=1.0)
    assert out[0]["suggestion"]["title"] == "Short"   # only Short fits 20m
    assert out[1]["suggestion"]["title"] == "Long"    # Short used; Long fits 60m


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


@pytest.fixture()
def client(store_open):
    app = create_app_with(store_open)
    with TestClient(app) as c:
        yield c


def create_app_with(store):
    from prefrontal.webhooks.app import create_app

    return create_app(store=store, settings=Settings(webhook_secret=SECRET))


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_todo_endpoints_crud_and_fit(client, store_open):
    """Create, list, fit, and close todos over HTTP."""
    store_open.set_state("time_estimation_bias", "1.0")  # keep fit math simple
    r = client.post("/todos", json={"title": "Call dentist", "estimate_minutes": 10,
                                     "priority": 2}, headers=_auth())
    assert r.status_code == 201
    tid = r.json()["todo_id"]

    client.post("/todos", json={"title": "Big project", "estimate_minutes": 90}, headers=_auth())

    listed = client.get("/todos", headers=_auth()).json()["todos"]
    assert {t["title"] for t in listed} == {"Call dentist", "Big project"}

    fit = client.get("/todos/fit", params={"minutes": 20}, headers=_auth()).json()
    assert [f["title"] for f in fit["fits"]] == ["Call dentist"]  # 90m one excluded

    done = client.post(f"/todos/{tid}/done", headers=_auth())
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert client.post(f"/todos/{tid}/done", headers=_auth()).status_code == 404


def test_todo_endpoints_require_auth(client):
    assert client.get("/todos").status_code == 401
    assert client.get("/todos/fit", params={"minutes": 10}).status_code == 401
    assert client.post("/todos", json={"title": "x"}).status_code == 401


# -- briefing tie-in ---------------------------------------------------------


def test_briefing_suggests_todos_in_spare_time(store):
    """The briefing proposes a fitting todo for an open window today."""
    from prefrontal.impact import utcnow

    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    # One commitment 10–11 today; rest of the 8–20 band is free.
    store.upsert_commitment(
        title="Meeting", start_at=_at(now.replace(hour=10)),
        end_at=_at(now.replace(hour=11)), external_id="work:1",
    )
    store.add_todo("Plan birthday", estimate_minutes=40, priority=2)
    b = build_briefing(store, now=now)
    assert any(s["suggestion"] == "Plan birthday" for s in b.spare)
