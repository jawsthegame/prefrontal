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
    WindowConfig,
    available_now,
    energy_time_rank,
    filter_suggestible,
    fit_todos,
    free_windows,
    parse_window,
    pick_now,
    resolve_window,
    suggest_for_windows,
    todo_allowed_at,
    window_config_for,
    work_window_now,
)
from prefrontal.todos import (
    KNOWN_CATEGORIES,
    MAX_CATEGORIES,
    at_category_cap,
    augment_todo,
    avoidance_score,
    avoided_todos,
    category_stats,
    decompose_task,
    heuristic_category,
    heuristic_deadline,
    heuristic_energy,
    heuristic_estimate,
    heuristic_priority,
    normalize_category,
    normalize_energy,
    resolve_category,
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


# -- categories --------------------------------------------------------------


def test_normalize_category_lowercases_and_defaults():
    assert normalize_category("  Home  Repairs ") == "home repairs"
    assert normalize_category("") == "other"
    assert normalize_category(None) == "other"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Call the dentist", "communication"),
        ("Pay the electric bill", "finance"),
        ("Buy groceries", "errands"),
        ("Book a doctor appointment", "health"),
        ("Refactor the auth code", "work"),
        ("Xyzzy plugh", "other"),
    ],
)
def test_heuristic_category(title, expected):
    assert heuristic_category(title) == expected


def test_category_windows_key_only_known_categories():
    """The scheduling windows must key real categories (single source of truth).

    Guards against vocabulary drift between todos.KNOWN_CATEGORIES and
    scheduling.DEFAULT_CATEGORY_WINDOWS — a typo/rename would otherwise silently
    never apply its window.
    """
    from prefrontal.scheduling import DEFAULT_CATEGORY_WINDOWS

    assert set(DEFAULT_CATEGORY_WINDOWS) <= set(KNOWN_CATEGORIES)


def test_at_category_cap_matches_resolve_category():
    """at_category_cap is the one cap rule; resolve_category remaps exactly when it's True."""
    under = ["work", "home"]
    assert at_category_cap("finance", under, cap=20) is False
    at_cap = [f"cat{i}" for i in range(MAX_CATEGORIES)]
    assert at_category_cap("brand-new", at_cap) is True   # novel + full -> blocked
    assert at_category_cap("cat5", at_cap) is False        # existing -> always allowed
    # The predicate normalizes, matching resolve_category's case-insensitivity.
    assert at_category_cap("CAT5", at_cap) is False


def test_resolve_category_allows_new_under_cap():
    assert resolve_category("finance", ["work", "home"], cap=20) == "finance"


def test_resolve_category_clamps_at_cap_to_existing():
    existing = [f"cat{i}" for i in range(MAX_CATEGORIES)]  # exactly at cap
    # A novel category can't be coined; falls back to the first (most-common).
    assert resolve_category("brand-new", existing, cap=MAX_CATEGORIES) == "cat0"
    # An already-existing one is still allowed at the cap.
    assert resolve_category("cat5", existing, cap=MAX_CATEGORIES) == "cat5"


def test_resolve_category_prefers_other_bucket_at_cap():
    existing = ["other"] + [f"cat{i}" for i in range(MAX_CATEGORIES - 1)]
    assert resolve_category("novel", existing, cap=MAX_CATEGORIES) == "other"


def test_normalize_energy():
    """Known levels are accepted case-insensitively; anything else is None."""
    assert normalize_energy("HIGH") == "high"
    assert normalize_energy(" low ") == "low"
    assert normalize_energy("banana") is None
    assert normalize_energy("") is None
    assert normalize_energy(None) is None
    assert normalize_energy(5) is None


def test_augment_keeps_valid_supplied_energy():
    aug = augment_todo("write report", energy="High", client=None)
    assert aug.energy == "high"
    assert aug.sources["energy"] == "stated"


def test_augment_rejects_out_of_vocab_supplied_energy():
    """An invalid supplied energy is inferred, never stored verbatim."""
    aug = augment_todo("email Bob", energy="banana", client=None)
    assert aug.energy in ("low", "medium", "high")  # inferred, not "banana"
    assert aug.sources["energy"] == "heuristic"


def test_augment_supplied_category_is_kept_and_clamped():
    aug = augment_todo("write report", category="Work Stuff", client=None)
    assert aug.category == "work stuff"
    assert aug.sources["category"] == "stated"


def test_augment_infers_category_via_heuristic_when_offline():
    aug = augment_todo("call the plumber", client=None)
    assert aug.category == "communication"
    assert aug.sources["category"] == "heuristic"


def test_augment_uses_llm_category_reusing_existing():
    client = _ollama_json(
        '{"estimate_minutes": 20, "priority": 1, "energy": "low", '
        '"deadline": null, "category": "finance"}'
    )
    aug = augment_todo("sort the invoices", existing_categories=["finance", "work"], client=client)
    assert aug.category == "finance"
    assert aug.sources["category"] == "llm"


def test_augment_llm_new_category_blocked_at_cap():
    """At the cap, even an LLM-proposed novel category is clamped to existing."""
    existing = [f"cat{i}" for i in range(MAX_CATEGORIES)]
    client = _ollama_json(
        '{"estimate_minutes": 20, "priority": 1, "energy": "low", '
        '"deadline": null, "category": "shiny-new"}'
    )
    aug = augment_todo("do a thing", existing_categories=existing, client=client)
    assert aug.category in existing


def test_category_stats_rollup():
    now = datetime(2026, 7, 1, 12, 0, 0)
    old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    todos = [
        {"category": "finance", "status": "open", "estimate_minutes": 10,
         "priority": 2, "created_at": old},
        {"category": "finance", "status": "done", "estimate_minutes": 20},
        {"category": "finance", "status": "dropped", "estimate_minutes": None},
        {"category": None, "status": "open", "estimate_minutes": 30},
    ]
    stats = category_stats(todos, now)
    by_cat = {s["category"]: s for s in stats}
    fin = by_cat["finance"]
    assert fin["open"] == 1 and fin["done"] == 1 and fin["dropped"] == 1
    assert fin["total"] == 3
    assert fin["avg_estimate_minutes"] == 15.0  # (10 + 20) / 2
    assert fin["completion_rate"] == 0.5  # 1 done of 2 closed
    assert fin["avoidance"] > 0  # the 10-day-old open one looks avoided
    assert by_cat["other"]["open"] == 1  # NULL category bucketed as "other"


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


def test_energy_time_rank_afternoon_prefers_low():
    """Mornings are energy-neutral; afternoons rank low-energy best."""
    assert energy_time_rank("high", 9) == energy_time_rank("low", 9) == 0  # morning neutral
    assert energy_time_rank("low", 15) == 0
    assert energy_time_rank("medium", 15) == 1
    assert energy_time_rank("high", 15) == 2


def test_pick_now_biases_toward_most_avoided_that_fits():
    """The most-avoided todo that fits wins, over a shinier quick one."""
    fits = [
        {"todo": {"id": 1, "energy": "low"}, "effective_minutes": 10},
        {"todo": {"id": 2, "energy": "high"}, "effective_minutes": 20},
    ]
    pick = pick_now(fits, avoided_ids=[2, 1], local_hour=15)
    assert (pick["todo"]["id"], pick["reason"]) == (2, "avoided")


def test_pick_now_prefers_low_energy_later_when_none_avoided():
    """With nothing avoided, afternoon prefers low-energy; morning keeps fit order."""
    fits = [
        {"todo": {"id": 1, "energy": "high"}, "effective_minutes": 10},
        {"todo": {"id": 2, "energy": "low"}, "effective_minutes": 20},
    ]
    assert pick_now(fits, [], local_hour=15)["todo"]["id"] == 2  # afternoon → low energy
    assert pick_now(fits, [], local_hour=9)["todo"]["id"] == 1  # morning → fit order kept


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


# -- category store + endpoints ----------------------------------------------


def test_store_category_roundtrip(store):
    tid = store.add_todo("Pay rent", category="finance")
    assert store.get_todo(tid)["category"] == "finance"
    store.add_todo("Call mom", category="communication")
    store.add_todo("Pay taxes", category="finance")
    # Ordered most-common-first: finance (2) before communication (1).
    assert store.todo_categories() == ["finance", "communication"]
    assert store.set_todo_category(tid, "home") is True
    assert store.get_todo(tid)["category"] == "home"
    assert store.set_todo_category(tid, None) is True
    assert store.get_todo(tid)["category"] is None
    assert store.set_todo_category(9999, "x") is False  # no such todo


def test_all_todos_includes_closed(store):
    a = store.add_todo("open one", category="work")
    b = store.add_todo("done one", category="work")
    store.close_todo(b, status="done")
    statuses = {t["title"]: t["status"] for t in store.all_todos()}
    assert statuses == {"open one": "open", "done one": "done"}
    assert a  # both present regardless of status


def test_create_todo_returns_category(client):
    r = client.post("/todos", json={"title": "email the team about launch"}, headers=_auth())
    assert r.status_code == 201
    # Offline ollama → heuristic: an email task → communication.
    assert r.json()["category"] == "communication"


def test_set_category_endpoint(client):
    tid = client.post("/todos", json={"title": "thing", "category": "work"},
                      headers=_auth()).json()["todo_id"]
    r = client.post(f"/todos/{tid}/category", json={"category": "Home Stuff"}, headers=_auth())
    assert r.status_code == 200 and r.json()["category"] == "home stuff"
    # Clear it.
    r = client.post(f"/todos/{tid}/category", json={"category": None}, headers=_auth())
    assert r.json()["category"] is None
    # Unknown todo → 404.
    assert client.post("/todos/9999/category", json={"category": "x"},
                       headers=_auth()).status_code == 404


def test_set_category_rejects_new_at_cap(client, store_open):
    # Fill the vocabulary to the cap with distinct categories.
    for i in range(MAX_CATEGORIES):
        store_open.add_todo(f"t{i}", category=f"cat{i}")
    tid = store_open.add_todo("edit me", category="cat0")
    # A brand-new category is refused (409); reusing an existing one is fine.
    assert client.post(f"/todos/{tid}/category", json={"category": "brand-new"},
                       headers=_auth()).status_code == 409
    assert client.post(f"/todos/{tid}/category", json={"category": "cat3"},
                       headers=_auth()).status_code == 200


def test_categories_endpoint_reports_stats_and_cap(client):
    client.post("/todos", json={"title": "pay bills", "category": "finance"}, headers=_auth())
    client.post("/todos", json={"title": "pay rent", "category": "finance"}, headers=_auth())
    r = client.get("/todos/categories", headers=_auth()).json()
    assert r["cap"] == MAX_CATEGORIES
    assert r["at_cap"] is False
    assert "finance" in r["categories"]
    fin = next(s for s in r["stats"] if s["category"] == "finance")
    assert fin["open"] == 2


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
    """backfill_added_columns back-fills done_steps on a pre-existing table."""
    import sqlite3

    from prefrontal.memory.migrate import backfill_added_columns

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE todo_decompositions (todo_id INTEGER PRIMARY KEY, first_step TEXT)")
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(todo_decompositions)")}
    assert "done_steps" in cols
    backfill_added_columns(conn)  # second run is a no-op, not an error
    conn.close()


# -- GET /todos/now (widget's "what fits right now") -------------------------


def _all_day(store):
    """Widen the working-hours band so the endpoint tests aren't clock-dependent."""
    store.set_state("time_estimation_bias", "1.0")
    store.set_state("fit_day_start", "00:00")
    store.set_state("fit_day_end", "23:59")


#: A fixed "now" for the /todos/now tests. Widening the day band alone isn't
#: enough: the free window is the gap from *now* to day-end, so a run late in the
#: real UTC day left <30 min and flaked. Freezing the endpoint's clock to midday
#: makes the remaining window deterministic (~12h, capped at DEFAULT_FIT_CAP).
_FROZEN_NOON = datetime(2026, 6, 15, 12, 0, 0)


def _freeze_todos_now_clock(monkeypatch, when=_FROZEN_NOON):
    """Pin the ``/todos/now`` route's ``utcnow()`` to a fixed instant."""
    monkeypatch.setattr("prefrontal.webhooks.routers.todos.utcnow", lambda: when)


def test_todos_now_suggests_fitting_todo(client, store_open, monkeypatch):
    """A short open todo with an open calendar is surfaced as the pick."""
    _all_day(store_open)
    _freeze_todos_now_clock(monkeypatch)
    client.post("/todos", headers=_auth(),
                json={"title": "Reply to landlord", "estimate_minutes": 30})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["within_hours"] is True
    assert r["free_minutes"] >= 30
    assert r["suggestion"]["title"] == "Reply to landlord"


def test_todos_now_none_when_nothing_fits(client, store_open, monkeypatch):
    """A todo longer than the (capped) free window yields no suggestion."""
    _all_day(store_open)
    _freeze_todos_now_clock(monkeypatch)
    client.post("/todos", headers=_auth(),
                json={"title": "Big project", "estimate_minutes": 300})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["suggestion"] is None
    assert r["reason"] == "nothing fits this window"


def test_todos_now_zero_when_busy(client, store_open, monkeypatch):
    """A commitment spanning now means no free time, so no suggestion."""
    _all_day(store_open)
    _freeze_todos_now_clock(monkeypatch)
    # Build the commitment around the frozen clock so it spans "now".
    client.post("/commitments", headers=_auth(), json={
        "title": "Meeting now",
        "start_at": _at(_FROZEN_NOON - timedelta(minutes=10)),
        "end_at": _at(_FROZEN_NOON + timedelta(minutes=30)),
    })
    client.post("/todos", headers=_auth(),
                json={"title": "Quick call", "estimate_minutes": 10})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["free_minutes"] == 0
    assert r["reason"] == "no free time right now"


def test_todos_now_requires_auth(client):
    assert client.get("/todos/now").status_code == 401


def test_todos_now_surfaces_the_avoided_thing(client, store_open, monkeypatch):
    """A week-old important todo beats a fresh shiny quick one (anti-avoidance)."""
    _all_day(store_open)
    _freeze_todos_now_clock(monkeypatch)
    client.post("/todos", headers=_auth(),
                json={"title": "Fun quick thing", "estimate_minutes": 10, "priority": 1})
    client.post("/todos", headers=_auth(),
                json={"title": "Call the accountant", "estimate_minutes": 15, "priority": 2})
    # Age the accountant todo past the avoidance threshold (open ≥ 3 days),
    # relative to the frozen clock so age is deterministic (8 days before now).
    aged = (_FROZEN_NOON - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    store_open.conn.execute(
        "UPDATE todos SET created_at = ? WHERE title = ?",
        (aged, "Call the accountant"),
    )
    store_open.conn.commit()
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["suggestion"]["title"] == "Call the accountant"
    assert r["suggestion"]["reason"] == "avoided"


# -- Suggestion time windows -------------------------------------------------


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_parse_window_valid_wrapping_and_malformed():
    """`parse_window` accepts HH:MM-HH:MM (wrap allowed) and rejects garbage."""
    assert parse_window("09:00-17:00") == (540, 1020)
    assert parse_window("22:00-06:00") == (1320, 360)  # wraps midnight
    assert parse_window("06:00-06:00") is None          # empty span
    assert parse_window("9-17") is None                  # not HH:MM
    assert parse_window("25:00-26:00") is None           # out of range
    assert parse_window(None) is None


def test_window_config_build_layers_and_awake_band():
    """State overrides env overrides built-in; awake band is the off-zone's complement."""
    config = WindowConfig.build(
        env_offzone="23:00-05:00",
        env_windows={"work": "08:00-16:00", "custom": "10:00-11:00"},
        state_offzone="22:00-06:00",       # state wins over env
        state_windows={"work": "09:00-17:00"},  # state wins over env
    )
    assert config.offzone == (_mins("22:00"), _mins("06:00"))
    assert config.windows["work"] == (_mins("09:00"), _mins("17:00"))
    assert config.windows["custom"] == (_mins("10:00"), _mins("11:00"))  # env-only key kept
    assert config.windows["home"] == (_mins("06:00"), _mins("22:00"))    # built-in default kept
    assert config.awake_band() == ("06:00", "22:00")


def test_window_config_build_ignores_malformed():
    """A malformed override falls back to the lower-precedence value, not a raise."""
    config = WindowConfig.build(env_offzone="nonsense", state_windows={"work": "oops"})
    assert config.offzone == parse_window("22:00-06:00")   # built-in default
    assert config.windows["work"] == (_mins("09:00"), _mins("17:00"))  # built-in default


def test_resolve_window_precedence():
    """per-todo override → category → source → default."""
    config = WindowConfig.build(env_windows={"impulse": "12:00-13:00"})
    # per-todo override wins over everything
    assert resolve_window(
        {"time_window": "05:00-06:00", "category": "work", "source": "impulse"}, config
    ) == (_mins("05:00"), _mins("06:00"))
    # category before source
    assert resolve_window({"category": "work", "source": "impulse"}, config) == (
        _mins("09:00"), _mins("17:00"),
    )
    # source when category is unknown
    assert resolve_window({"category": "nope", "source": "impulse"}, config) == (
        _mins("12:00"), _mins("13:00"),
    )
    # default when neither matches (the full waking band)
    assert resolve_window({"category": "other", "source": "manual"}, config) == (
        _mins("06:00"), _mins("22:00"),
    )


def test_todo_allowed_at_offzone_is_a_hard_gate():
    """Off-zone blocks even a todo whose own window would otherwise include the time."""
    config = WindowConfig.build()  # off-zone 22:00-06:00, default 06:00-22:00
    anytime = {"category": "home"}  # home window 06:00-22:00
    assert todo_allowed_at(anytime, datetime(2026, 6, 15, 12, 0), config) is True
    assert todo_allowed_at(anytime, datetime(2026, 6, 15, 3, 0), config) is False  # off-zone
    # A work todo is out-of-window in the evening but in-window midday.
    work = {"category": "work"}  # 09:00-17:00
    assert todo_allowed_at(work, datetime(2026, 6, 15, 20, 0), config) is False
    assert todo_allowed_at(work, datetime(2026, 6, 15, 10, 0), config) is True


def test_filter_suggestible_drops_out_of_window():
    """`filter_suggestible` keeps order and drops out-of-window todos."""
    config = WindowConfig.build()
    todos = [{"id": 1, "category": "work"}, {"id": 2, "category": "home"}]
    kept = filter_suggestible(todos, datetime(2026, 6, 15, 20, 0), config)
    assert [t["id"] for t in kept] == [2]  # work excluded at 8pm


def test_suggest_for_windows_respects_config():
    """With config+tz, a focus-hours todo isn't proposed for an evening gap."""
    config = WindowConfig.build()
    # A window at 20:00 UTC (local, tz=UTC) — evening.
    windows = [FreeWindow("2026-06-15 20:00:00", "2026-06-15 21:00:00", 60.0)]
    work_todo = [{"id": 1, "estimate_minutes": 30, "category": "work"}]
    out = suggest_for_windows(windows, work_todo, config=config, tz="UTC")
    assert out[0]["suggestion"] is None  # work not allowed at 8pm
    # Without config it ranks purely by fit (legacy behavior).
    out_legacy = suggest_for_windows(windows, work_todo)
    assert out_legacy[0]["suggestion"]["id"] == 1


def test_window_config_for_reads_settings_and_state(store):
    """`window_config_for` layers coaching-state over Settings env config."""
    settings = Settings(
        todo_offzone="21:00-07:00",
        todo_windows=(("work", "10:00-16:00"),),
    )
    store.set_state("todo_offzone", "20:00-08:00")       # state wins
    store.set_state("todo_window:work", "11:00-15:00")   # state wins
    config = window_config_for(settings, store)
    assert config.offzone == parse_window("20:00-08:00")
    assert config.windows["work"] == parse_window("11:00-15:00")


def test_todo_window_endpoint_set_clear_and_validation(client, store_open):
    """POST /todos/{id}/window sets, clears, and 422s on a malformed window."""
    tid = client.post("/todos", headers=_auth(), json={
        "title": "Water the plants", "estimate_minutes": 10,
        "time_window": "06:00-22:00",
    }).json()["todo_id"]
    assert store_open.get_todo(tid)["time_window"] == "06:00-22:00"
    # Override it via the dedicated endpoint.
    r = client.post(f"/todos/{tid}/window", headers=_auth(),
                    json={"time_window": "07:30-09:30"})
    assert r.status_code == 200
    assert store_open.get_todo(tid)["time_window"] == "07:30-09:30"
    # Malformed → 422, value unchanged.
    assert client.post(f"/todos/{tid}/window", headers=_auth(),
                       json={"time_window": "nope"}).status_code == 422
    # Clear it.
    client.post(f"/todos/{tid}/window", headers=_auth(), json={"time_window": None})
    assert store_open.get_todo(tid)["time_window"] is None


def test_todos_now_respects_category_window(client, store_open, monkeypatch):
    """At 8pm, a focus-hours todo is withheld while an anytime one is surfaced."""
    _freeze_todos_now_clock(monkeypatch, when=datetime(2026, 6, 15, 20, 0, 0))
    client.post("/todos", headers=_auth(),
                json={"title": "Draft the report", "estimate_minutes": 30})  # -> work 09-17
    client.post("/todos", headers=_auth(),
                json={"title": "Make coffee", "estimate_minutes": 10})       # -> other/default
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["suggestion"]["title"] == "Make coffee"


def test_todos_now_off_zone_offers_nothing(client, store_open, monkeypatch):
    """Inside the off-zone the widget is closed regardless of what fits."""
    _freeze_todos_now_clock(monkeypatch, when=datetime(2026, 6, 15, 23, 0, 0))
    client.post("/todos", headers=_auth(),
                json={"title": "Make coffee", "estimate_minutes": 10})
    r = client.get("/todos/now", headers=_auth()).json()
    assert r["within_hours"] is False
    assert r["suggestion"] is None
    assert r["reason"] == "outside waking hours"
