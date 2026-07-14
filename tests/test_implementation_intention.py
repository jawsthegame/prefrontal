"""Tests for the Implementation Intentions ("if-then plans") module.

Covers the pure cue-matching logic (time-of-day band + curated-place AND), the
``implementation_intentions`` store surface, the module evaluator (surfacing a
plan at its cue, and staying quiet otherwise), its integration with the coaching
engine's channel choice + debounce, and near-zero-friction capture through the NL
assistant. Model-free throughout — the technique's whole point is deterministic
delivery at the trigger, so there's nothing to stub.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prefrontal import assistant
from prefrontal.coaching import CoachContext, decide, record_fired
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.implementation_intention import (
    ImplementationIntentionModule,
    build_plan_message,
    plan_cue_active,
    within_window,
)

from .conftest import scoped_default

# The plan's place cue matches this curated place; a point far away doesn't.
_DESK_LAT, _DESK_LON = 40.700000, -73.900000
_FAR_LAT, _FAR_LON = 41.000000, -74.300000


@pytest.fixture()
def memory():
    conn = init_db(":memory:")
    store = MemoryStore(conn)
    scoped = scoped_default(store)
    try:
        yield scoped
    finally:
        conn.close()


def _seed_desk_plan(
    store: MemoryStore,
    *,
    cue_place: str | None = "desk",
    cue_window: str | None = "12:30-14:00",
) -> int:
    """A curated 'desk' place plus an if-then plan cued on it (and/or a window)."""
    if cue_place is not None:
        store.add_place(cue_place, _DESK_LAT, _DESK_LON, label="My desk")
    return store.add_implementation_intention(
        cue_text="I sit down at my desk after lunch",
        action_text="open the tax form and set a 5-min timer",
        cue_place=cue_place,
        cue_window=cue_window,
    )


def _ctx(*, hour: int = 13, lat: float | None = _DESK_LAT, lon: float | None = _DESK_LON):
    """A CoachContext at a UTC hour and (optional) location."""
    return CoachContext(
        now=datetime(2026, 7, 13, hour, 0),
        timezone="UTC",
        current_lat=lat,
        current_lon=lon,
    )


# --- pure cue matching -------------------------------------------------------


def test_within_window_inside_outside_and_wrap():
    at_one = datetime(2026, 7, 13, 13, 0)
    assert within_window(at_one, "12:30-14:00") is True
    assert within_window(at_one, "15:00-16:00") is False
    # Midnight-wrapping band: 13:00 is outside a 22:00–06:00 night window.
    assert within_window(at_one, "22:00-06:00") is False
    assert within_window(datetime(2026, 7, 13, 23, 0), "22:00-06:00") is True


def test_within_window_unparseable_is_fail_closed():
    # A window that can't be read never fires (rather than firing always).
    assert within_window(datetime(2026, 7, 13, 13, 0), "not-a-window") is False
    assert within_window(datetime(2026, 7, 13, 13, 0), None) is False


def test_plan_cue_active_both_constraints_are_anded():
    plan = {"cue_place": "desk", "cue_window": "12:30-14:00"}
    at_lunch = datetime(2026, 7, 13, 13, 0)
    off_hours = datetime(2026, 7, 13, 9, 0)
    assert plan_cue_active(plan, current_place_name="desk", local_dt=at_lunch) is True
    # Right place, wrong time → no fire.
    assert plan_cue_active(plan, current_place_name="desk", local_dt=off_hours) is False
    # Right time, wrong place → no fire.
    assert plan_cue_active(plan, current_place_name="gym", local_dt=at_lunch) is False
    # Right time, location unknown → the place constraint can't be met.
    assert plan_cue_active(plan, current_place_name=None, local_dt=at_lunch) is False


def test_plan_cue_active_place_only_and_window_only():
    place_only = {"cue_place": "desk", "cue_window": None}
    window_only = {"cue_place": None, "cue_window": "12:30-14:00"}
    at_lunch = datetime(2026, 7, 13, 13, 0)
    assert plan_cue_active(place_only, current_place_name="desk", local_dt=at_lunch) is True
    # A window-only plan fires regardless of (even absent) location.
    assert plan_cue_active(window_only, current_place_name=None, local_dt=at_lunch) is True
    off_hours = datetime(2026, 7, 13, 9, 0)
    assert plan_cue_active(window_only, current_place_name=None, local_dt=off_hours) is False


def test_plan_with_no_cue_never_fires():
    # A half-captured plan (no place, no window) stays quiet — nothing triggers it.
    plan = {"cue_place": None, "cue_window": None}
    at_lunch = datetime(2026, 7, 13, 13, 0)
    assert plan_cue_active(plan, current_place_name="desk", local_dt=at_lunch) is False


def test_build_plan_message_is_cue_first_and_gentle():
    msg = build_plan_message(
        {"cue_text": "I sit at my desk", "action_text": "open the tax form"}
    )
    assert "I sit at my desk" in msg
    assert "open the tax form" in msg
    # Forgiving tone: no streak/guilt language.
    for banned in ("missed", "failed", "streak", "again"):
        assert banned not in msg.lower()


# --- store round-trip --------------------------------------------------------


def test_store_add_active_get_and_status_filter(memory):
    pid = _seed_desk_plan(memory)
    active = memory.active_implementation_intentions()
    assert [p["id"] for p in active] == [pid]
    row = memory.get_implementation_intention(pid)
    assert row["cue_place"] == "desk"
    assert row["cue_window"] == "12:30-14:00"
    assert row["action_text"] == "open the tax form and set a 5-min timer"
    assert row["status"] == "active"
    assert row["last_fired_at"] is None
    assert memory.implementation_intentions(status="archived") == []


def test_store_archive_stops_it_firing(memory):
    pid = _seed_desk_plan(memory)
    assert memory.archive_implementation_intention(pid) is True
    assert memory.active_implementation_intentions() == []
    assert memory.get_implementation_intention(pid)["status"] == "archived"
    # Idempotent: re-archiving an already-archived plan reports no change.
    assert memory.archive_implementation_intention(pid) is False


def test_store_set_fired_stamp(memory):
    pid = _seed_desk_plan(memory)
    memory.set_implementation_intention_fired(pid, "2026-07-13 13:00:00")
    assert memory.get_implementation_intention(pid)["last_fired_at"].startswith(
        "2026-07-13 13:00"
    )
    # Unknown id is a silent no-op, never an error.
    memory.set_implementation_intention_fired(999, "2026-07-13 13:00:00")


# --- module evaluator --------------------------------------------------------


def test_evaluate_surfaces_plan_at_its_cue(memory):
    pid = _seed_desk_plan(memory)
    cues = ImplementationIntentionModule().evaluate(memory, _ctx())
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "implementation_intention"
    assert cue.intervention == "surface_at_cue"
    assert cue.urgency == "nudge"
    assert cue.dedup_key == f"if_then:{pid}"
    assert cue.ref == {"plan_id": pid}
    assert "open the tax form" in cue.text


def test_evaluate_silent_away_from_place(memory):
    _seed_desk_plan(memory)
    assert ImplementationIntentionModule().evaluate(memory, _ctx(lat=_FAR_LAT, lon=_FAR_LON)) == []


def test_evaluate_silent_outside_time_band(memory):
    _seed_desk_plan(memory)
    assert ImplementationIntentionModule().evaluate(memory, _ctx(hour=9)) == []


def test_evaluate_window_only_plan_fires_without_location(memory):
    _seed_desk_plan(memory, cue_place=None, cue_window="12:30-14:00")
    cues = ImplementationIntentionModule().evaluate(memory, _ctx(lat=None, lon=None))
    assert len(cues) == 1


def test_evaluate_archived_plan_excluded(memory):
    pid = _seed_desk_plan(memory)
    memory.archive_implementation_intention(pid)
    assert ImplementationIntentionModule().evaluate(memory, _ctx()) == []


def test_evaluate_no_plans_is_empty(memory):
    assert ImplementationIntentionModule().evaluate(memory, _ctx()) == []


def test_after_fire_stamps_last_fired(memory):
    pid = _seed_desk_plan(memory)
    module = ImplementationIntentionModule()
    ctx = _ctx()
    cues = module.evaluate(memory, ctx)
    decisions = decide(memory, cues, ctx)
    assert [d.cue.ref["plan_id"] for d in decisions] == [pid]
    module.after_fire(memory, decisions, ctx)
    assert memory.get_implementation_intention(pid)["last_fired_at"] is not None


# --- coaching-engine integration --------------------------------------------


def test_decide_routes_if_then_to_push_then_debounces(memory):
    _seed_desk_plan(memory)
    module = ImplementationIntentionModule()
    ctx = _ctx()

    cues = module.evaluate(memory, ctx)
    decisions = decide(memory, cues, ctx)
    assert len(decisions) == 1
    # A nudge floors to push (no learned channel history to bump it).
    assert decisions[0].channel == "push"

    # Once fired, the same cue is debounced on the next tick minutes later.
    record_fired(memory, decisions, ctx.now)
    soon = CoachContext(
        now=ctx.now + timedelta(minutes=5),
        timezone="UTC",
        current_lat=_DESK_LAT,
        current_lon=_DESK_LON,
    )
    again = decide(memory, module.evaluate(memory, soon), soon)
    assert again == []


def test_channel_targets_declares_if_then_ack():
    assert ImplementationIntentionModule().channel_targets() == {"if_then": "plan_id"}


# --- NL assistant capture ----------------------------------------------------


def test_assistant_captures_if_then_plan(memory):
    raw = [
        {
            "op": "add_if_then",
            "cue_text": "I sit down at my desk after lunch",
            "action_text": "open the tax form and set a 5-min timer",
            "place": "Desk",
            "time_window": "12:30-14:00",
        }
    ]
    actions, errors = assistant.validate_actions(raw, assistant.build_snapshot(memory))
    assert errors == []
    assert len(actions) == 1
    # The free-text place is normalized to the curated-place match key.
    assert actions[0].params["cue_place"] == "desk"
    assert actions[0].params["cue_window"] == "12:30-14:00"

    results = assistant.execute_actions(memory, actions)
    assert results[0]["ok"] is True
    plans = memory.active_implementation_intentions()
    assert len(plans) == 1
    assert plans[0]["action_text"] == "open the tax form and set a 5-min timer"


def test_assistant_requires_a_detectable_cue():
    actions, errors = assistant.validate_actions(
        [{"op": "add_if_then", "cue_text": "when I feel like it", "action_text": "start"}],
        {},
    )
    assert actions == []
    assert errors and "cue" in errors[0].lower()


def test_assistant_rejects_bad_time_window():
    actions, errors = assistant.validate_actions(
        [
            {
                "op": "add_if_then",
                "cue_text": "morning",
                "action_text": "stretch",
                "time_window": "whenever",
            }
        ],
        {},
    )
    assert actions == []
    assert errors and "time_window" in errors[0]


def test_assistant_add_if_then_is_whitelisted():
    assert "add_if_then" in assistant.ALLOWED_OPS
