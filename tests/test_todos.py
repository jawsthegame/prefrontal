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
from prefrontal.memory.patterns import recompute_patterns
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import (
    FreeWindow,
    available_now,
    fit_todos,
    free_windows,
    suggest_for_windows,
    work_window_now,
)
from prefrontal.todos import (
    augment_todo,
    avoidance_score,
    avoided_todos,
    decompose_task,
    heuristic_deadline,
    heuristic_energy,
    heuristic_estimate,
    heuristic_priority,
    todo_episode_fields,
)
from tests.conftest import scoped_default


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
        yield scoped_default(s)


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


# -- "free time right now" bounds --------------------------------------------

TZ = "America/New_York"  # June → EDT (UTC-4)


def test_work_window_now_inside_hours_capped():
    """Inside working hours, horizon = now + cap (when the day-end is further)."""
    now = datetime(2026, 6, 29, 15, 0, 0)  # 11:00 EDT — inside 08:30–17:30
    within, horizon = work_window_now(now, TZ, cap_minutes=90)
    assert within is True
    assert horizon == now + timedelta(minutes=90)


def test_work_window_now_bounded_by_day_end():
    """Near day-end, the horizon is clipped to the local 17:30, not now+cap."""
    now = datetime(2026, 6, 29, 21, 0, 0)  # 17:00 EDT — 30 min before 17:30
    within, horizon = work_window_now(now, TZ, cap_minutes=90)
    assert within is True
    assert horizon == datetime(2026, 6, 29, 21, 30, 0)  # 17:30 EDT → 21:30 UTC


@pytest.mark.parametrize("hour_utc", [11, 22])  # 07:00 EDT (early) and 18:00 EDT (late)
def test_work_window_now_outside_hours(hour_utc):
    """Outside the local band, within=False so nothing is offered."""
    now = datetime(2026, 6, 29, hour_utc, 0, 0)
    within, _ = work_window_now(now, TZ, cap_minutes=90)
    assert within is False


def test_available_now_gap_ongoing_and_open():
    """Free-until-next-commitment; 0 when busy now; full horizon when clear."""
    now = datetime(2026, 6, 29, 15, 0, 0)
    horizon = now + timedelta(minutes=90)
    soon = [{"start_at": _at(now + timedelta(minutes=30)),
             "end_at": _at(now + timedelta(minutes=60))}]
    assert available_now(soon, now, horizon) == 30  # free until the 30-min-out meeting
    ongoing = [{"start_at": _at(now - timedelta(minutes=10)),
                "end_at": _at(now + timedelta(minutes=20))}]
    assert available_now(ongoing, now, horizon) == 0  # in a meeting right now
    assert available_now([], now, horizon) == 90  # nothing on the calendar → full window


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
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
    llm = _ollama_json(
        '{"estimate_minutes": 25, "priority": 2, "energy": "high", "deadline": null}'
    )
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
    llm = _ollama_json(
        '{"estimate_minutes":30,"priority":1,"energy":"low","deadline":"2026-01-01"}'
    )
    # date(2026, 7, 1) is a Wednesday.
    a = augment_todo("File the report by Friday", client=llm, today=date(2026, 7, 1))
    assert a.deadline == "2026-07-03"          # the actual Friday
    assert a.sources["deadline"] == "heuristic"


def test_augment_deadline_falls_back_to_llm_when_heuristic_blank():
    """When the heuristic finds no relative term, the model's date is used."""
    llm = _ollama_json(
        '{"estimate_minutes":30,"priority":1,"energy":"low","deadline":"2026-08-15"}'
    )
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
    assert got["done_steps"] == []  # nothing ticked yet


def test_set_step_done_roundtrip_and_bounds(store):
    """Steps tick on/off by index (0 = first step); out-of-range no-ops."""
    tid = store.add_todo("Big task", estimate_minutes=90)
    assert store.set_step_done(tid, 0) is False  # no decomposition yet
    store.set_decomposition(
        tid, first_step="Open it", first_step_minutes=2, steps=["a", "b"], source="llm"
    )
    # Indices 0..2 are valid (first_step + 2 remaining).
    assert store.set_step_done(tid, 0) is True
    assert store.set_step_done(tid, 2) is True
    assert store.get_decomposition(tid)["done_steps"] == [0, 2]
    # Idempotent + un-tick.
    assert store.set_step_done(tid, 0) is True
    assert store.set_step_done(tid, 2, done=False) is True
    assert store.get_decomposition(tid)["done_steps"] == [0]
    # Out of range never records anything.
    assert store.set_step_done(tid, 3) is False
    assert store.set_step_done(tid, -1) is False
    assert store.get_decomposition(tid)["done_steps"] == [0]


def test_regenerating_decomposition_resets_step_progress(store):
    """Replacing a decomposition clears done_steps — the steps changed."""
    tid = store.add_todo("Big task", estimate_minutes=90)
    store.set_decomposition(
        tid, first_step="Open it", first_step_minutes=2, steps=["a"], source="llm"
    )
    store.set_step_done(tid, 1)
    assert store.get_decomposition(tid)["done_steps"] == [1]
    store.set_decomposition(
        tid, first_step="Open it", first_step_minutes=2, steps=["x", "y"], source="llm"
    )
    assert store.get_decomposition(tid)["done_steps"] == []


def test_update_todo_deadline_open_only(store):
    """Deadline moves and clears on open todos; closed todos no-op."""
    tid = store.add_todo("Renew passport", estimate_minutes=20, deadline="2026-07-01 00:00:00")
    assert store.update_todo_deadline(tid, "2026-08-15 09:00:00") is True
    assert store.get_todo(tid)["deadline"] == "2026-08-15 09:00:00"
    # Clearing.
    assert store.update_todo_deadline(tid, None) is True
    assert store.get_todo(tid)["deadline"] is None
    # Once closed, the deadline is frozen.
    store.close_todo(tid, status="done")
    assert store.update_todo_deadline(tid, "2026-09-01 00:00:00") is False
    assert store.update_todo_deadline(999, "2026-09-01 00:00:00") is False  # absent


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
    assert avoidance_score(
        _aged_todo(deadline="2026-06-28 12:00:00"), now
    ) > avoidance_score(_aged_todo(), now)


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


# -- outcome capture (closing a todo feeds the learning loop) -----------------


def test_todo_episode_fields_done_is_success():
    """A done todo becomes a task success; estimate is predicted, actual stays None."""
    fields = todo_episode_fields(
        {
            "title": "File taxes",
            "status": "done",
            "estimate_minutes": 30,
            "created_at": "2026-06-01 09:00:00",
            "completed_at": "2026-06-03 09:00:00",
        }
    )
    assert fields["episode_type"] == "task"
    assert fields["outcome"] == "success"
    assert fields["predicted_value"] == 30
    assert fields["actual_value"] is None  # wall-clock age must not pollute time_estimation
    assert fields["context"] == "todo done: File taxes"
    assert fields["notes"] == "completed after 2.0d open"


def test_todo_episode_fields_drop_is_miss_with_now_for_age():
    """A dropped todo (no completed_at) is a miss; age comes from the passed `now`."""
    fields = todo_episode_fields(
        {
            "title": "Read manual",
            "status": "dropped",
            "estimate_minutes": 20,
            "created_at": "2026-06-01 09:00:00",
            "completed_at": None,
        },
        now=datetime(2026, 6, 6, 9, 0, 0),
    )
    assert fields["outcome"] == "miss"
    assert fields["actual_value"] is None
    assert fields["notes"] == "dropped after 5.0d open"


def test_todo_close_endpoint_logs_episode(client, store_open):
    """Closing a todo over HTTP logs a task episode and returns its id."""
    tid = client.post(
        "/todos", json={"title": "Call dentist", "estimate_minutes": 10}, headers=_auth()
    ).json()["todo_id"]

    done = client.post(f"/todos/{tid}/done", headers=_auth())
    assert done.status_code == 200
    assert done.json()["episode_id"] is not None

    eps = store_open.episodes_by_type("task")
    assert len(eps) == 1
    assert eps[0]["outcome"] == "success"
    assert eps[0]["context"] == "todo done: Call dentist"


def test_todo_drop_endpoint_logs_miss(client, store_open):
    """Dropping a todo records a miss — the avoidance signal isn't discarded."""
    tid = client.post(
        "/todos", json={"title": "Reconcile budget", "estimate_minutes": 25}, headers=_auth()
    ).json()["todo_id"]

    client.post(f"/todos/{tid}/drop", headers=_auth())
    eps = store_open.episodes_by_type("task")
    assert eps and eps[0]["outcome"] == "miss"


def test_todo_closes_feed_drift_pattern(client, store_open):
    """Several closes flow into the learning pass as a `task` drift pattern."""
    for i in range(3):
        tid = client.post(
            "/todos", json={"title": f"task {i}", "estimate_minutes": 10}, headers=_auth()
        ).json()["todo_id"]
        client.post(f"/todos/{tid}/done", headers=_auth())
    tid = client.post(
        "/todos", json={"title": "skipped", "estimate_minutes": 10}, headers=_auth()
    ).json()["todo_id"]
    client.post(f"/todos/{tid}/drop", headers=_auth())

    recompute_patterns(store_open)
    drift = [p for p in store_open.get_patterns("drift") if p["context_key"] == "task"]
    assert drift, "expected a task drift pattern derived from todo closes"
    assert drift[0]["sample_size"] == 4
    # 3 successes (0.0) + 1 miss (1.0) over 4 ⇒ 0.25
    assert drift[0]["observed_value"] == 0.25
# -- deadline updates & step completion (endpoints) --------------------------


def test_update_deadline_endpoint(client, store_open):
    """POST /todos/{id}/deadline moves, clears, validates, and 404s as expected."""
    tid = store_open.add_todo("Renew passport", estimate_minutes=20)
    # Set a deadline (date-only is normalized to midnight UTC).
    r = client.post(f"/todos/{tid}/deadline", json={"deadline": "2026-08-15"}, headers=_auth())
    assert r.status_code == 200 and r.json()["deadline"] == "2026-08-15 00:00:00"
    assert store_open.get_todo(tid)["deadline"] == "2026-08-15 00:00:00"
    # Clear it.
    cleared = client.post(
        f"/todos/{tid}/deadline", json={"deadline": None}, headers=_auth()
    )
    assert cleared.json()["deadline"] is None
    # Garbage is rejected.
    bad = client.post(
        f"/todos/{tid}/deadline", json={"deadline": "not-a-date"}, headers=_auth()
    )
    assert bad.status_code == 422
    # Unknown / closed todo → 404.
    missing = client.post(
        "/todos/999/deadline", json={"deadline": "2026-08-15"}, headers=_auth()
    )
    assert missing.status_code == 404
    store_open.close_todo(tid, status="done")
    closed = client.post(
        f"/todos/{tid}/deadline", json={"deadline": "2026-08-15"}, headers=_auth()
    )
    assert closed.status_code == 404


def test_step_done_endpoint(client, store_open):
    """POST /todos/{id}/steps/{i}/done ticks steps and surfaces them in GET /todos."""
    tid = store_open.add_todo("Write the report", estimate_minutes=90)
    store_open.set_decomposition(
        tid,
        first_step="Open the doc",
        first_step_minutes=2,
        steps=["Draft intro", "Edit"],
        source="llm",
    )
    # Tick the first step (index 0) — default body marks it done.
    r = client.post(f"/todos/{tid}/steps/0/done", headers=_auth())
    assert r.status_code == 200 and r.json()["decomposition"]["done_steps"] == [0]
    # Tick a remaining step, then un-tick the first.
    client.post(f"/todos/{tid}/steps/2/done", headers=_auth())
    r = client.post(f"/todos/{tid}/steps/0/done", json={"done": False}, headers=_auth())
    assert r.json()["decomposition"]["done_steps"] == [2]
    # It surfaces in the list payload the dashboard renders.
    listed = client.get("/todos", headers=_auth()).json()["todos"]
    match = next(t for t in listed if t["id"] == tid)
    assert match["decomposition"]["done_steps"] == [2]
    # Out-of-range step → 404; the done/drop route still works (no collision).
    assert client.post(f"/todos/{tid}/steps/9/done", headers=_auth()).status_code == 404
    assert client.post(f"/todos/{tid}/done", headers=_auth()).status_code == 200


def test_migrate_adds_done_steps_idempotently():
    """_migrate back-fills done_steps on a pre-existing decomposition table."""
    import sqlite3

    from prefrontal.memory.db import _migrate

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE todo_decompositions (todo_id INTEGER PRIMARY KEY, first_step TEXT)")
    _migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(todo_decompositions)")}
    assert "done_steps" in cols
    _migrate(conn)  # second run is a no-op, not an error
    conn.close()


# -- GET /todos/now (widget's "what fits right now") -------------------------


def _all_day(store):
    """Widen the working-hours band so the endpoint tests aren't clock-dependent."""
    store.set_state("time_estimation_bias", "1.0")
    store.set_state("fit_day_start", "00:00")
    store.set_state("fit_day_end", "23:59")


def test_todos_now_suggests_fitting_todo(client, store_open):
    """A short open todo with an open calendar is surfaced as the pick."""
    _all_day(store_open)
    client.post("/todos", headers=_auth(),
                json={"title": "Reply to landlord", "estimate_minutes": 30})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["within_hours"] is True
    assert r["free_minutes"] >= 30
    assert r["suggestion"]["title"] == "Reply to landlord"


def test_todos_now_none_when_nothing_fits(client, store_open):
    """A todo longer than the (capped) free window yields no suggestion."""
    _all_day(store_open)
    client.post("/todos", headers=_auth(),
                json={"title": "Big project", "estimate_minutes": 300})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["suggestion"] is None
    assert r["reason"] == "nothing fits this window"


def test_todos_now_zero_when_busy(client, store_open):
    """A commitment spanning now means no free time, so no suggestion."""
    _all_day(store_open)
    now = datetime.utcnow()
    client.post("/commitments", headers=_auth(), json={
        "title": "Meeting now",
        "start_at": _at(now - timedelta(minutes=10)),
        "end_at": _at(now + timedelta(minutes=30)),
    })
    client.post("/todos", headers=_auth(),
                json={"title": "Quick call", "estimate_minutes": 10})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["free_minutes"] == 0
    assert r["reason"] == "no free time right now"


def test_todos_now_requires_auth(client):
    assert client.get("/todos/now").status_code == 401
