"""Tests for todos (open loops) and time-fitting.

Covers the todo store, the pure free-window / fitting functions, the endpoints,
and the morning-briefing "spare time" tie-in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.briefing import build_briefing, render_briefing
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import FreeWindow, fit_todos, free_windows, suggest_for_windows
from prefrontal.todos import (
    augment_todo,
    avoidance_score,
    avoided_todos,
    decompose_task,
    heuristic_deadline,
    heuristic_energy,
    heuristic_estimate,
    heuristic_priority,
)


def _offline_ollama() -> OllamaClient:
    """An OllamaClient whose calls fail — augment falls back to heuristics."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_json(text: str) -> OllamaClient:
    """An OllamaClient whose generate() returns `text` (the LLM augment path)."""
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))

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

    return create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )


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


# -- augmentation ------------------------------------------------------------


def test_heuristics_fill_fields_from_title():
    """Keyword heuristics give sensible estimate/priority/energy."""
    assert heuristic_estimate("Call the dentist") == 10.0
    assert heuristic_estimate("Draft the Q3 plan") == 45.0
    assert heuristic_estimate("Wander aimlessly") == 30.0  # default
    assert heuristic_priority("Pay rent ASAP") == 3
    assert heuristic_priority("Someday learn guitar") == 0
    assert heuristic_energy("Email Bob") == "low"
    assert heuristic_energy("Write the proposal") == "high"


def test_heuristic_deadline_relative_terms():
    """Relative deadlines parse against a reference date (Wed 2026-07-01)."""
    today = date(2026, 7, 1)  # Wednesday
    assert heuristic_deadline("ship it tomorrow", today) == "2026-07-02"
    assert heuristic_deadline("reply by Friday", today) == "2026-07-03"
    assert heuristic_deadline("call mom", today) is None


def test_augment_prefers_stated_then_llm_then_heuristic():
    """Supplied fields win; else the model; else heuristics."""
    llm = _ollama_json('{"estimate_minutes": 25, "priority": 2, "energy": "high", "deadline": null}')
    a = augment_todo("Call the dentist", priority=0, client=llm)
    assert a.priority == 0 and a.sources["priority"] == "stated"   # stated wins
    assert a.estimate_minutes == 25.0 and a.sources["estimate_minutes"] == "llm"
    assert a.energy == "high" and a.sources["energy"] == "llm"

    # No client → heuristics throughout.
    h = augment_todo("Call the dentist")
    assert (h.estimate_minutes, h.energy) == (10.0, "low")
    assert h.sources["estimate_minutes"] == "heuristic"


def test_post_todo_augments_missing_fields(client):
    """A bare title gets an inferred estimate so it becomes fit-able (offline → heuristic)."""
    r = client.post("/todos", json={"title": "Call the dentist"}, headers=_auth())
    assert r.status_code == 201
    body = r.json()
    assert body["estimate_minutes"] == 10.0
    assert body["energy"] == "low"
    assert body["augmented"]["estimate_minutes"] == "heuristic"
    # And it now shows up in a 15-minute fit (previously impossible with no estimate).
    fit = client.get("/todos/fit", params={"minutes": 15}, headers=_auth()).json()
    assert "Call the dentist" in [f["title"] for f in fit["fits"]]


def test_augment_deadline_prefers_heuristic_over_llm():
    """For a relative date the exact heuristic wins over the model's guess."""
    # Model returns a wrong date; "by Friday" heuristic should override it.
    llm = _ollama_json('{"estimate_minutes":30,"priority":1,"energy":"low","deadline":"2026-01-01"}')
    a = augment_todo("File the report by Friday", client=llm, today=date(2026, 7, 1))  # Wed
    assert a.deadline == "2026-07-03"          # the actual Friday
    assert a.sources["deadline"] == "heuristic"


def test_augment_deadline_falls_back_to_llm_when_heuristic_blank():
    """When the heuristic finds no relative term, the model's date is used."""
    llm = _ollama_json('{"estimate_minutes":30,"priority":1,"energy":"low","deadline":"2026-08-15"}')
    a = augment_todo("Submit taxes before the cutoff", client=llm, today=date(2026, 7, 1))
    assert a.deadline == "2026-08-15"
    assert a.sources["deadline"] == "llm"


# -- decomposition -----------------------------------------------------------


def test_decompose_task_llm_then_heuristic():
    """The model gives a first step + rest; offline falls back to a verb heuristic."""
    llm = _ollama_json(
        '{"first_step":"Open the doc and write one heading","first_step_minutes":3,'
        '"steps":["Draft section 1","Skim for typos"]}'
    )
    d = decompose_task("Write the annual report", client=llm)
    assert d.first_step == "Open the doc and write one heading"
    assert d.first_step_minutes == 3.0
    assert d.steps == ["Draft section 1", "Skim for typos"]
    assert d.source == "llm"

    h = decompose_task("Write the annual report")  # no client → heuristic
    assert "doc" in h.first_step.lower() and h.steps == [] and h.source == "heuristic"


def test_decompose_clamps_first_step_minutes():
    """An over-long first-step estimate is clamped to the max."""
    llm = _ollama_json('{"first_step":"Do it","first_step_minutes":999,"steps":[]}')
    d = decompose_task("Big thing", max_first_minutes=5, client=llm)
    assert d.first_step_minutes == 5.0


def test_decomposition_store_roundtrip(store):
    """set/get_decomposition round-trips with steps JSON-decoded."""
    tid = store.add_todo("Big task", estimate_minutes=90)
    assert store.get_decomposition(tid) is None
    store.set_decomposition(
        tid, first_step="Just open it", first_step_minutes=2, steps=["a", "b"], source="llm"
    )
    got = store.get_decomposition(tid)
    assert got["first_step"] == "Just open it" and got["steps"] == ["a", "b"]


def test_post_big_todo_auto_decomposes(client):
    """A big todo (heuristic estimate ≥ threshold) gets a stored first step."""
    r = client.post("/todos", json={"title": "Write the project proposal"}, headers=_auth())
    body = r.json()
    assert body["estimate_minutes"] >= 30
    assert body["decomposition"] is not None and body["decomposition"]["first_step"]
    listed = client.get("/todos", headers=_auth()).json()["todos"]
    match = next(t for t in listed if t["title"] == "Write the project proposal")
    assert match["decomposition"]["first_step"]


def test_small_todo_no_auto_but_on_demand_and_no_route_collision(client):
    """A small todo doesn't auto-decompose; the on-demand route works and
    doesn't collide with done/drop."""
    r = client.post("/todos", json={"title": "Call Bob"}, headers=_auth())  # ~10 min
    body = r.json()
    assert body["decomposition"] is None
    tid = body["todo_id"]

    d = client.post(f"/todos/{tid}/decompose", headers=_auth())
    assert d.status_code == 200 and d.json()["decomposition"]["first_step"]

    assert client.post(f"/todos/{tid}/done", headers=_auth()).status_code == 200


# -- avoidance detection -----------------------------------------------------


def _aged_todo(**kw):
    base = {"id": 1, "title": "Call insurance", "status": "open", "priority": 2,
            "estimate_minutes": 10, "deadline": None, "created_at": "2026-06-25 12:00:00"}
    base.update(kw)
    return base


def test_avoidance_scoring_and_exemptions():
    now = datetime(2026, 7, 1, 12, 0, 0)  # 6 days after the default created_at
    avoided = avoided_todos([_aged_todo()], now)
    assert avoided and avoided[0]["todo"]["id"] == 1 and avoided[0]["days_open"] == 6.0

    # Low-priority "someday" items are exempt.
    assert avoided_todos([_aged_todo(id=2, priority=0)], now) == []
    # Fresh todos aren't avoidance yet (under min_days).
    assert avoided_todos([_aged_todo(id=3, created_at="2026-06-30 12:00:00")], now) == []
    # An overdue deadline amplifies the score.
    assert avoidance_score(_aged_todo(deadline="2026-06-28 12:00:00"), now) > avoidance_score(_aged_todo(), now)


def test_todos_avoided_endpoint_and_flag(client, store_open):
    old = store_open.add_todo("Call insurance", estimate_minutes=10, priority=2)
    store_open.conn.execute(
        "UPDATE todos SET created_at = ? WHERE id = ?", ("2020-01-01 00:00:00", old)
    )
    store_open.conn.commit()
    store_open.add_todo("Just added", estimate_minutes=10, priority=2)  # fresh

    avoided = client.get("/todos/avoided", headers=_auth()).json()["avoided"]
    assert [a["title"] for a in avoided] == ["Call insurance"]

    todos = client.get("/todos", headers=_auth()).json()["todos"]
    flagged = {t["title"]: t["avoidance"] is not None for t in todos}
    assert flagged["Call insurance"] is True
    assert flagged["Just added"] is False


def test_briefing_surfaces_avoided(store):
    from prefrontal.impact import utcnow

    now = utcnow()
    tid = store.add_todo("Renew passport", estimate_minutes=20, priority=2)
    store.conn.execute(
        "UPDATE todos SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"), tid),
    )
    store.conn.commit()
    b = build_briefing(store, now=now)
    assert any(a["title"] == "Renew passport" for a in b.avoided)
    assert "keep putting off" in render_briefing(b).lower()
