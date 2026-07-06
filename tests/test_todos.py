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
from prefrontal.clock import utcnow
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.patterns import recompute_patterns
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import (
    TRAVEL_LATEST_HOUR,
    FreeWindow,
    WindowConfig,
    available_now,
    energy_time_rank,
    filter_suggestible,
    first_window_fitting,
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
    DISCARDED_OUTCOME,
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
    historical_drop_is_hygiene,
    normalize_category,
    normalize_energy,
    reclassify_hygiene_drops,
    requires_travel,
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


def test_first_window_fitting_picks_earliest_that_fits():
    """The soonest window long enough wins; too-short earlier ones are skipped."""
    windows = [
        FreeWindow("2026-06-29 09:00:00", "2026-06-29 09:15:00", 15),
        FreeWindow("2026-06-29 11:00:00", "2026-06-29 12:00:00", 60),
        FreeWindow("2026-06-29 14:00:00", "2026-06-29 15:00:00", 60),
    ]
    assert first_window_fitting(windows, 30).start == "2026-06-29 11:00:00"  # skips the 15m
    assert first_window_fitting(windows, 10).start == "2026-06-29 09:00:00"  # 15m fits
    assert first_window_fitting(windows, 90) is None  # nothing that long


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


def test_suggest_for_windows_multi_option():
    """options_per_window returns a menu per gap; the primary is options[0]."""
    windows = [FreeWindow("2026-06-29 09:00:00", "2026-06-29 10:00:00", 60)]
    todos = [
        {"id": 1, "title": "A", "estimate_minutes": 10, "priority": 1, "deadline": None},
        {"id": 2, "title": "B", "estimate_minutes": 20, "priority": 1, "deadline": None},
        {"id": 3, "title": "C", "estimate_minutes": 30, "priority": 1, "deadline": None},
    ]
    out = suggest_for_windows(windows, todos, bias=1.0, options_per_window=3)
    options = out[0]["options"]
    assert len(options) == 3
    assert out[0]["suggestion"] is options[0]  # primary is the first option


def test_suggest_for_windows_primary_reserved_options_advisory():
    """The primary never repeats across windows; alternatives may."""
    windows = [FreeWindow("2026-06-29 09:00:00", "2026-06-29 10:00:00", 60),
               FreeWindow("2026-06-29 13:00:00", "2026-06-29 14:00:00", 60)]
    todos = [
        {"id": 1, "title": "A", "estimate_minutes": 10, "priority": 2, "deadline": None},
        {"id": 2, "title": "B", "estimate_minutes": 20, "priority": 1, "deadline": None},
    ]
    out = suggest_for_windows(windows, todos, bias=1.0, options_per_window=2)
    # Distinct primaries (reserved), but B is a valid alternative in both gaps.
    assert out[0]["suggestion"]["id"] != out[1]["suggestion"]["id"]
    assert any(t["id"] == 2 for t in out[0]["options"])


def test_fit_todos_bias_fn_is_per_todo():
    """bias_fn pads each todo by its own multiplier (§5), overriding the flat bias."""
    todos = [
        {"id": 1, "title": "heavy", "estimate_minutes": 20, "energy": "high",
         "priority": 1, "deadline": None},
        {"id": 2, "title": "light", "estimate_minutes": 20, "energy": "low",
         "priority": 1, "deadline": None},
    ]
    # High-energy tasks blow estimates (2×) so "heavy" overflows a 30m block;
    # low-energy is on the nose (1×) so "light" fits.
    by_energy = {"high": 2.0, "low": 1.0}
    fits = fit_todos(30, todos, bias_fn=lambda t: by_energy[t["energy"]])
    titles = [f["todo"]["title"] for f in fits]
    assert titles == ["light"]  # heavy: 20*2=40 > 30; light: 20*1=20 ≤ 30


def test_suggest_for_windows_resolver_is_per_window_and_per_todo():
    """resolver_for_hour builds a per-todo bias keyed on each window's local hour (§5)."""
    # UTC == local (tz set, no config) so the local hour is the UTC hour.
    windows = [FreeWindow("2026-06-29 09:00:00", "2026-06-29 09:30:00", 30),
               FreeWindow("2026-06-29 18:00:00", "2026-06-29 18:30:00", 30)]
    todos = [{"id": 1, "title": "T", "estimate_minutes": 20, "energy": "high",
              "priority": 1, "deadline": None}]
    # Bias depends on BOTH the window hour and the todo's energy: morning 1.0×,
    # evening 2.0× — so the same todo fits the morning gap but overflows the evening.
    def resolver_for_hour(hour):
        mult = {9: 1.0, 18: 2.0}[hour]
        return lambda todo: mult if todo["energy"] == "high" else 1.0
    out = suggest_for_windows(
        windows, todos, bias=1.0, tz="UTC", resolver_for_hour=resolver_for_hour
    )
    assert out[0]["suggestion"]["title"] == "T"   # morning: 20*1.0=20 ≤ 30 fits
    assert out[1]["suggestion"] is None           # evening: 20*2.0=40 > 30 overflows


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


def test_schedule_todo_at_explicit_time(client, store_open):
    """Scheduling with an explicit `at` blocks the bias-adjusted estimate as a commitment."""
    store_open.set_state("time_estimation_bias", "1.0")  # keep the math simple
    tid = client.post(
        "/todos", json={"title": "Write report", "estimate_minutes": 45}, headers=_auth()
    ).json()["todo_id"]
    r = client.post(
        f"/todos/{tid}/schedule", json={"at": "2026-07-10 14:00:00"}, headers=_auth()
    )
    assert r.status_code == 200
    body = r.json()
    assert body["start_at"] == "2026-07-10 14:00:00"
    assert body["end_at"] == "2026-07-10 14:45:00"  # 45m estimate × 1.0 bias
    # A real commitment now exists, and the todo is still open (blocked, not done).
    commits = client.get("/commitments", headers=_auth()).json()["commitments"]
    assert any(c["title"] == "Write report" for c in commits)
    assert any(t["id"] == tid for t in client.get("/todos", headers=_auth()).json()["todos"])


def test_schedule_todo_minutes_override_and_errors(client, store_open):
    """`minutes` overrides the estimate; missing-estimate and unknown-todo error cleanly."""
    tid = client.post("/todos", json={"title": "Sketch idea"}, headers=_auth()).json()["todo_id"]
    # Explicit minutes wins over any inferred estimate.
    r = client.post(
        f"/todos/{tid}/schedule", json={"at": "2026-07-10 14:00:00", "minutes": 20},
        headers=_auth(),
    )
    assert r.json()["end_at"] == "2026-07-10 14:20:00"
    # Unknown / closed todo → 404.
    assert client.post("/todos/9999/schedule", json={"at": "2026-07-10 14:00:00"},
                       headers=_auth()).status_code == 404


def test_schedule_todo_auto_finds_free_window(client, store_open, monkeypatch):
    """With no `at`, it places the block in the earliest fitting free window today."""
    from prefrontal.impact import utcnow

    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("prefrontal.webhooks.routers.todos.utcnow", lambda: now)
    store_open.set_state("time_estimation_bias", "1.0")
    tid = client.post(
        "/todos", json={"title": "Deep work", "estimate_minutes": 30}, headers=_auth()
    ).json()["todo_id"]
    r = client.post(f"/todos/{tid}/schedule", json={}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["minutes"] == 30 and body["start_at"][11:16] == "09:00"


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


def test_set_domain_endpoint(client):
    """POST /todos/{id}/domain sets, normalizes, clears the work/life guardrail; 404s unknown."""
    tid = client.post("/todos", json={"title": "quarterly report"},
                      headers=_auth()).json()["todo_id"]
    r = client.post(f"/todos/{tid}/domain", json={"domain": "  Work  "}, headers=_auth())
    assert r.status_code == 200 and r.json() == {"todo_id": tid, "domain": "work"}
    # Clear it (empty string normalizes to null, same as omitting).
    r = client.post(f"/todos/{tid}/domain", json={"domain": ""}, headers=_auth())
    assert r.status_code == 200 and r.json()["domain"] is None
    r = client.post(f"/todos/{tid}/domain", json={"domain": None}, headers=_auth())
    assert r.status_code == 200 and r.json()["domain"] is None
    # Unknown todo → 404.
    assert client.post("/todos/9999/domain", json={"domain": "home"},
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


# -- dismissing a breakdown (+ the learning it feeds) -------------------------


def test_dismiss_decomposition_records_feedback_and_removes_it(store):
    """A repo-level dismissal captures a snapshot, then drops the decomposition."""
    tid = store.add_todo("Write the report", estimate_minutes=90, priority=2)
    store.set_decomposition(
        tid, first_step="Open the doc", first_step_minutes=3, steps=["Draft"], source="llm"
    )
    d = store.get_decomposition(tid)
    store.record_decomposition_dismissal(
        todo_id=tid, title="Write the report", reason="not_useful", source=d["source"],
        first_step=d["first_step"], steps=d["steps"], category=None, estimate_minutes=90,
    )
    assert store.delete_decomposition(tid) is True
    assert store.get_decomposition(tid) is None  # gone
    assert store.delete_decomposition(tid) is False  # nothing left to delete
    fb = store.decomposition_feedback_list()
    assert len(fb) == 1
    assert fb[0]["reason"] == "not_useful" and fb[0]["steps"] == ["Draft"]


def test_learned_guidance_uses_only_not_useful(store):
    """The decomposer addendum draws on 'not_useful' dismissals, not 'not_needed'."""
    from prefrontal.todos import learned_decomposition_guidance

    assert learned_decomposition_guidance(store) == ""  # nothing learned yet
    store.record_decomposition_dismissal(
        todo_id=None, title="Book dentist", reason="not_needed",
        first_step="Find the number", steps=[], estimate_minutes=40,
    )
    assert learned_decomposition_guidance(store) == ""  # not_needed doesn't feed the prompt
    store.record_decomposition_dismissal(
        todo_id=None, title="Write the report", reason="not_useful",
        first_step="Think about it", steps=[], estimate_minutes=90,
    )
    guidance = learned_decomposition_guidance(store)
    assert "unhelpful" in guidance and "Write the report" in guidance
    assert "Book dentist" not in guidance


def test_auto_decompose_suppressed_after_repeated_not_needed(store):
    """Enough 'not_needed' dismissals switch off auto-decompose (learn when not to)."""
    from prefrontal.todos import DEFAULT_DECOMP_SUPPRESS_THRESHOLD, auto_decompose_suppressed

    assert auto_decompose_suppressed(store) is False
    for i in range(DEFAULT_DECOMP_SUPPRESS_THRESHOLD):
        store.record_decomposition_dismissal(
            todo_id=None, title=f"task {i}", reason="not_needed", first_step="do it",
            steps=[], estimate_minutes=60,
        )
    assert auto_decompose_suppressed(store) is True
    # An operator override of 0 disables suppression entirely.
    store.set_state("decomposition_suppress_threshold", "0")
    assert auto_decompose_suppressed(store) is False


def test_dismiss_decomposition_endpoint(client, store_open):
    """POST /decompose/dismiss records feedback, removes the breakdown, 404s when none."""
    tid = client.post(
        "/todos", json={"title": "Write report", "estimate_minutes": 90}, headers=_auth()
    ).json()["todo_id"]
    # Decomposition isn't auto-made at creation now; create one on demand to dismiss.
    client.post(f"/todos/{tid}/decompose", headers=_auth())
    assert store_open.get_decomposition(tid) is not None
    r = client.post(
        f"/todos/{tid}/decompose/dismiss", json={"reason": "not_needed"}, headers=_auth()
    )
    assert r.status_code == 200 and r.json()["dismissed"] is True
    assert store_open.get_decomposition(tid) is None
    assert store_open.decomposition_dismissed_count(reason="not_needed") == 1
    # Dismissing again → 404 (nothing to dismiss).
    assert client.post(
        f"/todos/{tid}/decompose/dismiss", json={"reason": "not_needed"}, headers=_auth()
    ).status_code == 404
    # An unknown reason is rejected.
    assert client.post(
        f"/todos/{tid}/decompose/dismiss", json={"reason": "meh"}, headers=_auth()
    ).status_code == 422


def test_decompose_task_allow_decline():
    """With allow_decline the model can veto a breakdown; without it, never None."""
    from prefrontal.todos import decompose_task

    # Explicit model decline → None, but only when the caller opted in.
    assert decompose_task(
        "Reply 'yes' to Sam", client=_ollama_json('{"decompose": false}'), allow_decline=True
    ) is None
    assert decompose_task(
        "Reply 'yes' to Sam", client=_ollama_json('{"decompose": false}')
    ) is not None  # first-step callers still get a (heuristic) breakdown
    # A usable step comes back as an llm decomposition.
    d = decompose_task(
        "Write report",
        client=_ollama_json(
            '{"first_step":"Open the doc","first_step_minutes":3,"steps":["Draft"]}'
        ),
        allow_decline=True,
    )
    assert d is not None and d.first_step == "Open the doc" and d.source == "llm"
    # No model can't judge, so an avoided task still gets a heuristic first step.
    assert decompose_task("Write report", client=None, allow_decline=True) is not None


def _breaks_down():
    return _ollama_json('{"first_step":"Open it","first_step_minutes":2,"steps":["a"]}')


def test_sweep_decomposes_only_avoided_tasks(store):
    """The sweep breaks down avoided todos, not fresh ones."""
    from prefrontal.todos import sweep_avoided_decompositions

    tid = store.add_todo("A big task", estimate_minutes=90, priority=2)
    # Right now it's fresh → nothing avoided → no-op.
    assert sweep_avoided_decompositions(store, _breaks_down(), now=utcnow()) == 0
    assert store.get_decomposition(tid) is None
    # Ten days on it's avoided → the model breaks it down.
    future = utcnow() + timedelta(days=10)
    assert sweep_avoided_decompositions(store, _breaks_down(), now=future) == 1
    assert store.get_decomposition(tid)["first_step"] == "Open it"
    # A second sweep is a no-op — it already has a breakdown.
    assert sweep_avoided_decompositions(store, _breaks_down(), now=future) == 0


def test_sweep_records_and_skips_model_declines(store):
    """A model decline is recorded once so later sweeps don't re-ask."""
    from prefrontal.todos import sweep_avoided_decompositions

    tid = store.add_todo("Trivial avoided thing", estimate_minutes=90, priority=2)
    future = utcnow() + timedelta(days=10)
    decline = _ollama_json('{"decompose": false}')
    assert sweep_avoided_decompositions(store, decline, now=future) == 0
    assert store.get_decomposition(tid) is None
    assert tid in store.decomposition_feedback_todo_ids()  # decline remembered
    # Even if the model would now break it down, the decided todo is skipped.
    assert sweep_avoided_decompositions(store, _breaks_down(), now=future) == 0
    assert store.get_decomposition(tid) is None


def test_sweep_no_op_when_suppressed(store):
    """Repeated 'not needed' dismissals switch the whole sweep off."""
    from prefrontal.todos import sweep_avoided_decompositions

    store.set_state("decomposition_suppress_threshold", "1")
    store.record_decomposition_dismissal(
        todo_id=None, title="prior", reason="not_needed", first_step="x", steps=[]
    )
    store.add_todo("Big avoided thing", estimate_minutes=120, priority=2)
    future = utcnow() + timedelta(days=10)
    assert sweep_avoided_decompositions(store, _breaks_down(), now=future) == 0


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


def test_post_todo_never_auto_decomposes_at_creation(client):
    """A big todo is no longer broken down at creation — that waits until it's
    avoided (the coaching sweep), so a fresh task stays uncluttered."""
    r = client.post("/todos", json={"title": "Write the project proposal"}, headers=_auth())
    body = r.json()
    assert body["estimate_minutes"] >= 30
    assert body["decomposition"] is None
    listed = client.get("/todos", headers=_auth()).json()["todos"]
    match = next(t for t in listed if t["title"] == "Write the project proposal")
    assert match.get("decomposition") is None


def test_on_demand_decompose_works_and_no_route_collision(client):
    """The on-demand route breaks a task down and doesn't collide with done/drop."""
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

    # Deadlines are stored *date-only* (YYYY-MM-DD) — the actual production
    # format. Regression: these went through parse_ts (which needs a full
    # timestamp) and silently returned None, so deadline pressure never applied.
    plain = avoidance_score(_aged_todo(), now)
    assert avoidance_score(_aged_todo(deadline="2026-06-28"), now) > plain  # overdue ×3
    assert avoidance_score(_aged_todo(deadline="2026-07-02"), now) > plain  # imminent ×2


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
    text = render_briefing(b)
    assert "Keeps sliding" in text and "Renew passport" in text


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


def test_todo_drop_of_fresh_todo_is_discarded_not_a_miss(client, store_open):
    """A just-created drop is hygiene ("this is wrong"), not a slip: it's logged
    as `discarded`, which feeds neither drift nor the briefing's Slipped line."""
    tid = client.post(
        "/todos", json={"title": "Reconcile budget", "estimate_minutes": 25}, headers=_auth()
    ).json()["todo_id"]

    client.post(f"/todos/{tid}/drop", headers=_auth())
    eps = store_open.episodes_by_type("task")
    assert eps and eps[0]["outcome"] == DISCARDED_OUTCOME


def test_todo_drop_of_overdue_todo_is_a_miss(client, store_open):
    """Dropping a commitment you let go overdue reads as "I give up" → a miss, so
    the avoidance signal isn't lost."""
    tid = store_open.add_todo("File the taxes", estimate_minutes=25, deadline="2020-01-01")
    client.post(f"/todos/{tid}/drop", headers=_auth())
    eps = store_open.episodes_by_type("task")
    assert eps and eps[0]["outcome"] == "miss"


def test_todo_episode_fields_classifies_drops(store_open):
    """The give-up (miss) vs hygiene (discarded) split, at the pure layer."""
    now = utcnow()

    def dropped(**kw):
        base = {"status": "dropped", "title": "t", "priority": 1,
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S")}
        return {**base, **kw}

    old = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    # Done is always a success.
    assert todo_episode_fields({"status": "done", "title": "t"}, now=now)["outcome"] == "success"
    # Aged past the avoidance floor at a real priority → give up (miss).
    assert todo_episode_fields(dropped(created_at=old), now=now)["outcome"] == "miss"
    # Overdue → give up (miss), regardless of age.
    assert todo_episode_fields(dropped(deadline="2020-01-01"), now=now)["outcome"] == "miss"
    # Fresh, not overdue → hygiene (discarded).
    assert todo_episode_fields(dropped(), now=now)["outcome"] == DISCARDED_OUTCOME
    # Low-priority "someday", even if aged → hygiene, never avoidance.
    assert (
        todo_episode_fields(dropped(created_at=old, priority=0), now=now)["outcome"]
        == DISCARDED_OUTCOME
    )
    # No reference time → can't date it → treat as hygiene, not a slip.
    assert todo_episode_fields(dropped(created_at=old), now=None)["outcome"] == DISCARDED_OUTCOME


def test_todo_closes_feed_drift_pattern(client, store_open):
    """Successes + a give-up miss flow into the learning pass as a `task` drift
    pattern; a hygiene drop is excluded, so it can't inflate drift."""
    for i in range(3):
        tid = client.post(
            "/todos", json={"title": f"task {i}", "estimate_minutes": 10}, headers=_auth()
        ).json()["todo_id"]
        client.post(f"/todos/{tid}/done", headers=_auth())
    # An overdue drop = a genuine give-up miss.
    gid = store_open.add_todo("skipped", estimate_minutes=10, deadline="2020-01-01")
    client.post(f"/todos/{gid}/drop", headers=_auth())
    # A fresh hygiene drop → discarded → NOT counted in drift.
    hid = client.post(
        "/todos", json={"title": "mis-captured", "estimate_minutes": 10}, headers=_auth()
    ).json()["todo_id"]
    client.post(f"/todos/{hid}/drop", headers=_auth())

    recompute_patterns(store_open)
    drift = [p for p in store_open.get_patterns("drift") if p["context_key"] == "task"]
    assert drift, "expected a task drift pattern derived from todo closes"
    assert drift[0]["sample_size"] == 4  # 3 successes + 1 give-up miss; hygiene excluded
    # 3 successes (0.0) + 1 miss (1.0) over 4 ⇒ 0.25
    assert drift[0]["observed_value"] == 0.25


# -- historical hygiene-drop cleanup -----------------------------------------


def test_historical_drop_is_hygiene_reads_the_age_note():
    """The classifier keys off the "dropped after Xd open" note the drop wrote."""
    assert historical_drop_is_hygiene({"notes": "dropped after 0.5d open"})
    # At/over the avoidance floor → left a miss (may be a real give-up).
    assert not historical_drop_is_hygiene({"notes": "dropped after 3.0d open"})
    assert not historical_drop_is_hygiene({"notes": "dropped after 12.0d open"})
    # A completed note, or no note at all, is never a hygiene drop.
    assert not historical_drop_is_hygiene({"notes": "completed after 0.2d open"})
    assert not historical_drop_is_hygiene({"notes": None})
    assert not historical_drop_is_hygiene({})


def _log_drop(store, *, title, age_days, outcome="miss"):
    """Log a `todo dropped:` episode with the age note the fix writes."""
    return store.log_episode(
        "task",
        context=f"todo dropped: {title}",
        outcome=outcome,
        notes=f"dropped after {age_days:.1f}d open",
    )


def test_reclassify_hygiene_drops_only_touches_fresh_misses(store_open):
    """A fresh drop miss → discarded; an aged one and an outing miss are left."""
    fresh = _log_drop(store_open, title="mis-captured", age_days=0.5)
    aged = _log_drop(store_open, title="File taxes", age_days=6.0)
    # A non-todo miss that happens to be a task episode must be untouched.
    other = store_open.log_episode(
        "task", context="outing abandoned: gym", outcome="miss", notes="45m out"
    )

    dry = reclassify_hygiene_drops(store_open, apply=False)
    assert dry == {"scanned": 2, "reclassified": 1, "samples": ["todo dropped: mis-captured"]}
    # Dry run wrote nothing.
    assert store_open.get_episode(fresh)["outcome"] == "miss"

    applied = reclassify_hygiene_drops(store_open, apply=True)
    assert applied["scanned"] == 2 and applied["reclassified"] == 1
    assert store_open.get_episode(fresh)["outcome"] == DISCARDED_OUTCOME
    assert store_open.get_episode(aged)["outcome"] == "miss"
    assert store_open.get_episode(other)["outcome"] == "miss"


def test_reclassify_hygiene_drops_is_idempotent(store_open):
    """A second apply finds nothing left (the rewritten row is no longer a miss)."""
    fresh = _log_drop(store_open, title="quick clear", age_days=0.2)
    assert reclassify_hygiene_drops(store_open, apply=True)["reclassified"] == 1

    again = reclassify_hygiene_drops(store_open, apply=True)
    assert again == {"scanned": 0, "reclassified": 0, "samples": []}
    assert store_open.get_episode(fresh)["outcome"] == DISCARDED_OUTCOME


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


def test_todos_now_suggestion_carries_the_domain(client, store_open, monkeypatch):
    """The widget's pick echoes the todo's work/life domain so it can label it.

    An unknown domain doesn't gate scheduling (it falls through to category /
    default, per resolve_window), so the pick surfaces exactly as the base case —
    but the stored domain now rides along in the suggestion payload.
    """
    _all_day(store_open)
    _freeze_todos_now_clock(monkeypatch)
    r = client.post("/todos", headers=_auth(),
                    json={"title": "Reply to landlord", "estimate_minutes": 30})
    tid = r.json()["todo_id"]
    assert client.post(f"/todos/{tid}/domain", json={"domain": "misc"},
                       headers=_auth()).status_code == 200
    sug = client.get("/todos/now", headers=_auth()).json()["suggestion"]
    assert sug["title"] == "Reply to landlord"
    assert sug["domain"] == "misc"


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


def test_resolve_window_domain_outranks_category():
    """Work/life guardrail: domain beats category, but a per-todo override beats domain."""
    config = WindowConfig.build(env_windows={"work": "09:00-17:00", "home": "06:00-22:00"})
    # A work-domain todo triaged as "home" is still held to work hours.
    assert resolve_window({"domain": "work", "category": "home"}, config) == (
        _mins("09:00"), _mins("17:00"),
    )
    # An explicit per-todo window still wins over the domain.
    assert resolve_window(
        {"time_window": "05:00-06:00", "domain": "work"}, config
    ) == (_mins("05:00"), _mins("06:00"))
    # An unknown domain falls through to category.
    assert resolve_window({"domain": "mystery", "category": "home"}, config) == (
        _mins("06:00"), _mins("22:00"),
    )


def test_work_domain_todo_is_gated_out_of_the_evening():
    """A work-domain todo is suggestible in work hours, blocked after them."""
    config = WindowConfig.build(env_windows={"work": "09:00-17:00"})
    work_todo = {"domain": "work", "category": "communication"}
    assert todo_allowed_at(work_todo, datetime(2026, 6, 15, 10, 0), config) is True
    assert todo_allowed_at(work_todo, datetime(2026, 6, 15, 20, 0), config) is False


def test_crunch_active_self_expires():
    from datetime import timedelta, timezone

    from prefrontal.scheduling import _crunch_active

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    past = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert _crunch_active(future) is True
    assert _crunch_active(past) is False
    assert _crunch_active("") is False
    assert _crunch_active(None) is False
    assert _crunch_active("not a timestamp") is False


def test_crunch_mode_suspends_bands_but_keeps_offzone():
    """Crunch lets a work todo surface after hours, but never in the off-zone."""
    base = WindowConfig.build(env_windows={"work": "09:00-17:00"})
    crunch = WindowConfig.build(env_windows={"work": "09:00-17:00"}, crunch=True)
    work = {"domain": "work"}
    # 8pm: outside the work band → blocked normally, allowed under crunch.
    assert todo_allowed_at(work, datetime(2026, 6, 15, 20, 0), base) is False
    assert todo_allowed_at(work, datetime(2026, 6, 15, 20, 0), crunch) is True
    # 3am: the off-zone is a hard gate even in crunch.
    assert todo_allowed_at(work, datetime(2026, 6, 15, 3, 0), crunch) is False


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


@pytest.mark.parametrize(
    "title, travel",
    [
        ("Pick up dry cleaning", True),
        ("Drive to the DMV", True),
        ("Grab groceries", True),
        ("Run errands", True),
        ("Drop off the package", True),
        ("Call the dentist", False),        # a phone call, not travel
        ("Order printer ink online", False),  # online, no physical-presence cue
        ("Draft the report", False),
    ],
)
def test_requires_travel_is_title_based(title, travel):
    assert requires_travel({"title": title}) is travel


def test_todo_allowed_at_travel_capped_at_6pm():
    """A travel-requiring todo is gated at 18:00 even though its window runs later."""
    config = WindowConfig.build()  # errands window 09:00-20:00
    errand = {"category": "errands", "title": "Pick up prescription"}
    # Fine in the afternoon…
    assert todo_allowed_at(errand, datetime(2026, 6, 15, 16, 0), config) is True
    # …but not at 6pm+ (before the errands window's own 20:00 close).
    assert todo_allowed_at(errand, datetime(2026, 6, 15, 18, 0), config) is False
    assert todo_allowed_at(errand, datetime(2026, 6, 15, 19, 0), config) is False
    # A non-travel todo in the same category/window is still fine at 7pm.
    deskbound = {"category": "errands", "title": "Compare insurance quotes online"}
    assert todo_allowed_at(deskbound, datetime(2026, 6, 15, 19, 0), config) is True
    assert TRAVEL_LATEST_HOUR == 18


def test_suggest_for_windows_skips_travel_in_evening():
    """A travel errand isn't proposed for a 7pm gap, but a desk task is."""
    config = WindowConfig.build()
    windows = [FreeWindow("2026-06-15 19:00:00", "2026-06-15 20:00:00", 60.0)]
    travel = [{"id": 1, "estimate_minutes": 30, "title": "Drive to the store"}]
    assert suggest_for_windows(windows, travel, config=config, tz="UTC")[0]["suggestion"] is None
    desk = [{"id": 2, "estimate_minutes": 30, "title": "Reply to Sam"}]
    assert suggest_for_windows(windows, desk, config=config, tz="UTC")[0]["suggestion"] is not None


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
    assert config.crunch is False  # no crunch_until set


def test_window_config_for_picks_up_active_crunch(store):
    """A future `crunch_until` in coaching-state flips the config into crunch mode."""
    from datetime import timedelta, timezone

    future = (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=6)
    ).strftime("%Y-%m-%d %H:%M:%S")
    store.set_state("crunch_until", future)
    assert window_config_for(Settings(), store).crunch is True


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


def test_todo_notes_endpoint_set_clear_and_missing(client, store_open):
    """POST /todos/{id}/notes sets, clears (blank/None), and 404s on a closed/absent todo."""
    tid = client.post("/todos", headers=_auth(), json={
        "title": "Call the accountant", "notes": "needs the account number",
    }).json()["todo_id"]
    assert store_open.get_todo(tid)["notes"] == "needs the account number"

    r = client.post(f"/todos/{tid}/notes", headers=_auth(),
                    json={"notes": "  bring last year's return  "})
    assert r.status_code == 200 and r.json()["notes"] == "bring last year's return"
    assert store_open.get_todo(tid)["notes"] == "bring last year's return"

    # Blank clears; the todo surfaces the cleared value.
    client.post(f"/todos/{tid}/notes", headers=_auth(), json={"notes": "   "})
    assert store_open.get_todo(tid)["notes"] is None

    # A closed todo isn't editable (404); an absent one too.
    client.post(f"/todos/{tid}/done", headers=_auth())
    assert client.post(f"/todos/{tid}/notes", headers=_auth(),
                       json={"notes": "x"}).status_code == 404
    assert client.post("/todos/99999/notes", headers=_auth(),
                       json={"notes": "x"}).status_code == 404


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
