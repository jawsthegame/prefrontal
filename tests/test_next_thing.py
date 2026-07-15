"""Tests for "one next thing" — the single honest next action.

Covers the resolution ladder (leave-now > mid-flight > overdue fire > leave-soon >
due-soon fire > avoided > fits > clear), the mid-flight pin (and its one override),
the "and N more can wait" subtraction, the deterministic rendering, and the
``GET /next`` endpoint.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.next_thing import _departure_thing, build_next_thing, render_next_thing
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "next-secret"


def _at(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def noon():
    """A fixed midday 'now' so windowing never depends on wall-clock time."""
    return utcnow().replace(hour=12, minute=0, second=0, microsecond=0)


# --- The ladder --------------------------------------------------------------


def test_all_clear_when_nothing_pressing(store, noon):
    """An empty board is itself the answer — a calm invitation, not a void."""
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "clear"
    assert thing.reason == "clear"
    assert thing.also_count == 0
    assert "breath" in thing.headline.lower()


def test_mid_flight_focus_is_pinned_over_a_fire(store, noon):
    """In a focus block, the glance reflects it back — it does not yank you to a fire."""
    # An overdue todo is a genuine fire, but you're mid-flight: don't switch you.
    store.add_todo("File the taxes", deadline=(noon - timedelta(days=2)).strftime("%Y-%m-%d"))
    store.start_focus_session("the API refactor", started_at=_at(noon - timedelta(minutes=25)))
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "focus"
    assert thing.reason == "mid-flight"
    assert thing.title == "the API refactor"
    assert thing.action == "wrap_up"
    # The overdue todo is withheld, not lost — it counts toward "N more can wait".
    assert thing.also_count >= 1


def test_mid_flight_outing_is_pinned(store, noon):
    """Out on an errand → the next move is that trip, with a one-tap 'I'm back'."""
    store.start_outing("getting coffee", 20.0, departure_at=_at(noon - timedelta(minutes=10)))
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "outing"
    assert thing.reason == "mid-flight"
    assert thing.title == "getting coffee"
    assert thing.action == "im_back"


def test_leave_now_overrides_a_mid_flight_task():
    """A commitment you must physically leave for *now* beats even flow."""
    # Real-clock relative: plan_upcoming_departures reads the DB's CURRENT_TIMESTAMP,
    # so anchor the commitment to now, not a fixed fixture.
    now = utcnow()
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        store.start_focus_session("deep work")  # mid-flight, would normally pin
        store.upsert_commitment(
            title="Dentist", start_at=_at(now + timedelta(minutes=2)),
            lead_minutes=30, external_id="personal:1", hardness="hard",
        )
        thing = build_next_thing(store)
    assert thing.kind == "departure"
    assert thing.reason == "leave-now"
    assert thing.title == "Leave for Dentist"
    assert thing.action == "leave"


def test_leave_soon_sits_below_an_overdue_fire():
    """A leave-soon commitment is real, but an already-overdue todo outranks it."""
    now = utcnow()
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        # Leaving in ~8 min (soon), and a todo that's been overdue for a day.
        store.upsert_commitment(
            title="Standup", start_at=_at(now + timedelta(minutes=18)),
            lead_minutes=10, external_id="w:1",
        )
        store.add_todo(
            "Pay the invoice", deadline=(now - timedelta(days=1)).strftime("%Y-%m-%d")
        )
        thing = build_next_thing(store)
    assert thing.kind == "todo"
    assert thing.reason == "overdue"
    assert thing.title == "Pay the invoice"


def test_mid_flight_pins_the_most_recent_session(store, noon):
    """With two active focus sessions, the latest in-flight one is pinned — not the
    oldest (the active-* lists are ordered oldest-first)."""
    store.start_focus_session("the old thing", started_at=_at(noon - timedelta(hours=2)))
    store.start_focus_session("the current thing", started_at=_at(noon - timedelta(minutes=5)))
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "focus"
    assert thing.title == "the current thing"


def test_leave_soon_detail_rounds_up_never_zero():
    """A sub-minute leave-soon must read "leave in 1 min", not "leave in 0 min" /
    "leave now" — that would contradict the soon-not-go semantics."""
    dep = SimpleNamespace(
        commitment={"id": 1, "title": "Standup", "location": None, "calendar": "work"},
        minutes_until_leave=0.4,
    )
    thing = _departure_thing(dep, reason="leave-soon", also_count=0)
    assert thing.detail == "leave in 1 min"
    assert "in 1 min" in thing.headline
    assert "leave now" not in thing.detail


def test_overdue_todo_is_the_next_thing(store, noon):
    """With nothing mid-flight and no departure, the worst overdue todo surfaces."""
    store.add_todo(
        "Renew the passport", deadline=(noon - timedelta(days=3)).strftime("%Y-%m-%d")
    )
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "todo"
    assert thing.reason == "overdue"
    assert thing.action == "start"
    assert "overdue" in thing.detail


def test_blocker_past_due_reads_as_someone_waiting(store, noon):
    """A person past-due on you surfaces as 'waiting', the ball in your court."""
    store.add_blocker(
        "Sam", "the budget numbers",
        deadline=_at(noon - timedelta(hours=3)),
        blocking_since=_at(noon - timedelta(days=1)),
    )
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "blocker"
    assert thing.reason == "waiting"
    assert "Sam" in thing.title


def test_urgent_mail_surfaces_when_no_hard_clock(store, noon):
    """Urgent action-mail is the next thing when nothing has a harder clock."""
    store.record_mail(
        account="work", message_id="m1", sender_name="Boss", subject="Contract???",
        needs_action=True, urgency="urgent",
    )
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "mail"
    assert thing.reason == "urgent-mail"
    assert thing.source == "work"


def test_avoided_but_important_when_nothing_is_pressing():
    """No clock anywhere → name the thing you keep skipping, not the shiny one."""
    now = utcnow()
    later = (now + timedelta(days=6)).replace(hour=12, minute=0, second=0, microsecond=0)
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        # Open ~6 days, high priority, no deadline → avoided (panic's piling_up).
        store.add_todo("Sort the garage", priority=2)
        thing = build_next_thing(store, now=later)
    assert thing.kind == "todo"
    assert thing.reason == "avoided"
    assert thing.title == "Sort the garage"
    assert "skipping" in thing.detail


def test_fits_the_window_when_fresh_and_free(store, noon):
    """A fresh todo with an estimate that fits the free window is offered to start."""
    store.add_todo("Draft the memo", estimate_minutes=15, priority=1)
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "todo"
    assert thing.reason == "fits"
    assert thing.title == "Draft the memo"
    assert thing.estimate_minutes == 15
    assert thing.free_minutes and thing.free_minutes > 0


# --- Subtraction / rendering -------------------------------------------------


def test_also_count_hides_the_rest_of_the_mountain(store, noon):
    """The chosen thing is shown; everything else collapses to a single count."""
    # Three overdue todos: one is chosen, two become "and 2 more can wait".
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    thing = build_next_thing(store, now=noon)
    assert thing.kind == "todo"
    assert thing.also_count == 2


def test_render_is_one_action_plus_a_reassuring_tail(store, noon):
    """The CLI render shows one action, its honest reason, and the 'N more' line."""
    for t in ("A", "B"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    text = render_next_thing(build_next_thing(store, now=noon))
    assert "One next thing" in text
    assert "▶" in text
    assert "can wait" in text


def test_render_all_clear_is_calm(store, noon):
    """The all-clear render says so plainly and stops — no empty list scaffolding."""
    text = render_next_thing(build_next_thing(store, now=noon))
    assert "All clear" not in text  # not a bucket header; it's a calm sentence
    assert "Nothing pressing" in text
    assert "▶" not in text


# --- Endpoint ----------------------------------------------------------------


def test_next_endpoint(store):
    """GET /next returns the single-thing payload, token-guarded."""
    # A focus session pins deterministically regardless of the wall clock.
    store.start_focus_session("the quarterly report")
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        assert c.get("/next").status_code == 401
        body = c.get("/next", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["kind"] == "focus"
    assert body["reason"] == "mid-flight"
    assert body["title"] == "the quarterly report"
    assert body["action"] == "wrap_up"
    assert "text" in body and "One next thing" in body["text"]
