"""Tests for the self-care module — the meal ("have you eaten?") + water checks.

Covers the cue producer (:class:`~prefrontal.modules.self_care.SelfCareModule`)
on the coaching tick for both the once-daily meal check and the recurring water
check, the one-tap actions via ``GET /nudge/act`` (Ate / Snooze / Drank), and
that ``/webhooks/coach/check`` surfaces the cues with their signed buttons.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.coaching import CoachContext, Cue, Decision
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import (
    SELF_CARE_EPISODE,
    SNOOZED_UNTIL_KEY,
    SelfCareModule,
    adapt_self_care,
    adapt_self_care_interval,
    apply_self_care_action,
    apply_self_care_config,
    apply_self_care_mark,
    apply_self_care_reset,
    apply_self_care_unmark,
    biobreak_message,
    mark_self_care_prompted,
    meal_message,
    self_care_status,
    sweep_unanswered_self_care,
    water_message,
)
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.oauth import sign_action, verify_action
from tests.conftest import DEFAULT_HANDLE, scoped_default

SECRET = "self-care-secret"
SIGNING = "self-care-signing"
BASE = "https://mac-mini.tailnet.ts.net"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    app = create_app(
        store=store,
        settings=Settings(
            webhook_secret=SECRET,
            session_secret=SIGNING,
            oauth_base_url=BASE,
            timezone="UTC",
        ),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _ctx(now: datetime, *, name: str = "") -> CoachContext:
    return CoachContext(now=now, timezone="UTC", display_name=name)


def _by_kind(cues, context_key):
    return next((c for c in cues if c.context_key == context_key), None)


# -- module.evaluate ---------------------------------------------------------


def test_messages_greet_by_name():
    assert meal_message("Tom").startswith("Tom, quick check")
    assert meal_message().startswith("Quick check")
    assert water_message("Tom").startswith("Tom, hydration check")


def test_off_by_default(store):
    """No cue unless the self_care key is explicitly on."""
    ctx = _ctx(datetime(2026, 7, 2, 15, 0, 0))
    assert SelfCareModule().evaluate(store, ctx) == []


def test_default_checks_fire_when_enabled(store):
    """With the master on, the default-on checks (meal, water, bio breaks) all fire
    in their windows; meds stays off until opted in."""
    store.set_state("self_care", "on")
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0), name="Tom"))
    assert {c.context_key for c in cues} == {"meal", "water", "biobreak"}


def test_meds_off_by_default_but_opt_in_fires(store):
    """Meds stays silent even with self_care on until meds_enabled is set."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    at_10 = _ctx(datetime(2026, 7, 2, 10, 0, 0), name="Tom")
    assert SelfCareModule().evaluate(store, at_10) == []  # off by default
    store.set_state("meds_enabled", "on")
    cues = SelfCareModule().evaluate(store, at_10)
    assert [c.context_key for c in cues] == ["meds"]
    assert cues[0].intervention == "meds_check"
    assert "meds" in cues[0].text.lower()


def test_act_meds_took_counts_and_logs(client, store):
    token = sign_action(DEFAULT_HANDLE, "meds_took", 20260702, SIGNING)
    assert client.get(f"/nudge/act?t={token}").status_code == 200
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("meds_count") == f"{today}|1"
    eps = store.episodes_by_type("self_care")
    assert eps and eps[0]["outcome"] == "confirmed" and "meds" in eps[0]["context"]


def test_meal_fires_when_due(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0), name="Tom"))
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "self_care"
    assert cue.intervention == "meal_check"
    assert cue.urgency == "nudge"
    assert cue.context_key == "meal"
    assert "eaten" in cue.text.lower()
    assert cue.ref["target"] == 20260702


def test_water_is_recurring_and_starts_earlier(store):
    """Water fires from 9:00 (before the meal window) and has no daily 'done'."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    # 10:00 — after water's 9:00 start but before the meal's 11:00.
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 10, 0, 0)))
    assert [c.context_key for c in cues] == ["water"]
    assert cues[0].intervention == "water_check"


def test_meal_silent_before_start_hour(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    # 09:00 is before the default 11:00 meal start.
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 9, 0, 0))) == []


def test_meal_silent_once_target_met(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    store.set_state("meal_count", "2026-07-02|1")  # target is 1 — done for the day
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0))) == []
    # A new day re-arms it (the count is date-scoped).
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 3, 15, 0, 0))), "meal")


def test_water_stops_after_daily_target(store):
    """Water keeps firing until the daily target of confirms is reached."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    now = _ctx(datetime(2026, 7, 2, 10, 0, 0))
    store.set_state("water_count", "2026-07-02|5")  # one short of the default 6
    assert _by_kind(SelfCareModule().evaluate(store, now), "water")
    store.set_state("water_count", "2026-07-02|6")  # target met
    assert SelfCareModule().evaluate(store, now) == []


def test_meal_silent_while_snoozed(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    now = datetime(2026, 7, 2, 15, 0, 0)
    store.set_state(SNOOZED_UNTIL_KEY, (now + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"))
    assert SelfCareModule().evaluate(store, _ctx(now)) == []
    # Past the snooze it fires again.
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(now + timedelta(minutes=25))), "meal")


def test_reask_bucket_advances_dedup_key(store):
    """Same re-ask window → same dedup_key; a later window → a new one."""
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    mod = SelfCareModule()
    k1 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0)))[0].dedup_key
    k2 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 20, 0)))[0].dedup_key
    k3 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 45, 0)))[0].dedup_key
    assert k1 == k2  # within the same 40-min window
    assert k3 != k1  # a later window


# -- bio breaks: a recurring check bounded by an end hour (every 2h, 8:00–19:00) --


def _biobreak_only(store):
    """Enable self-care with only the bio-break check on (it's opt-in)."""
    store.set_state("self_care", "on")
    for other in ("meal", "water", "meds"):
        store.set_state(f"{other}_enabled", "off")
    store.set_state("biobreak_enabled", "on")


def test_biobreak_on_by_default_and_can_be_disabled(store):
    """Bio breaks fire with self_care on (like meal/water); biobreak_enabled=off silences it."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("water_enabled", "off")
    at_10 = _ctx(datetime(2026, 7, 2, 10, 0, 0), name="Tom")
    cues = SelfCareModule().evaluate(store, at_10)
    assert [c.context_key for c in cues] == ["biobreak"]  # on by default
    assert cues[0].intervention == "biobreak_check"
    assert "break" in cues[0].text.lower()
    store.set_state("biobreak_enabled", "off")
    assert SelfCareModule().evaluate(store, at_10) == []


def test_biobreak_silent_before_start_and_after_end_hour(store):
    """The window is the point: nothing before 8:00 or at/after the 19:00 end hour."""
    _biobreak_only(store)
    mod = SelfCareModule()
    assert mod.evaluate(store, _ctx(datetime(2026, 7, 2, 7, 30, 0))) == []   # before start
    assert _by_kind(mod.evaluate(store, _ctx(datetime(2026, 7, 2, 12, 0, 0))), "biobreak")  # midday
    assert mod.evaluate(store, _ctx(datetime(2026, 7, 2, 19, 0, 0))) == []   # at the end hour
    assert mod.evaluate(store, _ctx(datetime(2026, 7, 2, 21, 0, 0))) == []   # well past it


def test_biobreak_end_hour_is_configurable(store):
    """A custom end hour narrows the window — quiet once past it."""
    _biobreak_only(store)
    store.set_state("biobreak_end_hour", "14")
    mod = SelfCareModule()
    assert _by_kind(mod.evaluate(store, _ctx(datetime(2026, 7, 2, 13, 0, 0))), "biobreak")
    assert mod.evaluate(store, _ctx(datetime(2026, 7, 2, 14, 0, 0))) == []


def test_biobreak_message_greets_by_name():
    assert biobreak_message("Tom").startswith("Tom, quick break")
    assert biobreak_message().startswith("Quick break")


def test_status_reports_end_hour_only_for_biobreak(store):
    """end_hour is present (int) for bio breaks, None for the time-unbounded checks."""
    status = self_care_status(store, datetime(2026, 7, 2, 12, 0, 0), "UTC")
    by_key = {c["key"]: c for c in status["checks"]}
    assert by_key["biobreak"]["end_hour"] == 19
    assert by_key["meal"]["end_hour"] is None
    assert by_key["water"]["end_hour"] is None


def test_biobreak_not_overdue_past_its_window(store):
    """A recurring check that's behind pace still clears its overdue flag past the end hour."""
    _biobreak_only(store)
    # 18:00 with nothing logged: behind pace, so flagged overdue inside the window…
    inside = self_care_status(store, datetime(2026, 7, 2, 18, 0, 0), "UTC")
    assert {c["key"]: c for c in inside["checks"]}["biobreak"]["overdue"] is True
    # …but at/after 19:00 the window has closed — it's quiet, not "behind".
    past = self_care_status(store, datetime(2026, 7, 2, 19, 30, 0), "UTC")
    assert {c["key"]: c for c in past["checks"]}["biobreak"]["overdue"] is False


def test_config_sets_biobreak_end_hour_and_ignores_it_elsewhere(store):
    """POST-style config writes end_hour for bio breaks; a no-op for checks without one."""
    apply_self_care_config(store, checks={"biobreak": {"end_hour": 20}, "water": {"end_hour": 20}})
    assert store.get_state("biobreak_end_hour") == "20"
    # Water has no end-hour key, so the field is silently ignored (no stray state).
    assert store.get_state("water_end_hour") is None


def test_biobreak_is_open_ended_no_count_ever_silences_it(store):
    """Bio breaks are a reminder, not a quota — a high count never stops the nudge.

    A quota check (water) goes quiet once its daily target is met; the open-ended
    bio-break keeps firing within its window no matter how many are logged.
    """
    _biobreak_only(store)
    today = datetime(2026, 7, 2).strftime("%Y-%m-%d")
    # Log well past the old daily target of 6 — a quota would be long done.
    store.set_state("biobreak_count", f"{today}|20")
    at_noon = _ctx(datetime(2026, 7, 2, 12, 0, 0))
    assert _by_kind(SelfCareModule().evaluate(store, at_noon), "biobreak")  # still nudging


def test_biobreak_went_defers_an_interval_but_is_never_done(store):
    """'Went' spaces the next reminder out and logs the response, but never 'done'.

    Unlike a quota confirm (which can end the check for the day), an open-ended
    confirm only defers — status never reports the bio-break as done, and it
    resumes nudging once the deferral elapses.
    """
    _biobreak_only(store)
    now = datetime(2026, 7, 2, 12, 0, 0)
    today = now.strftime("%Y-%m-%d")
    msg = apply_self_care_action(store, "biobreak_went", now=now, today=today)
    assert msg and "🚻" in msg  # confirmation copy, no "done for the day" framing
    # Counted (as an informational tally) and the next reminder pushed a full
    # interval out (default 120 min), so it's quiet right after the tap…
    assert store.get_state("biobreak_count") == f"{today}|1"
    deferred = datetime.strptime(store.get_state("biobreak_snoozed_until"), "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=115) < (deferred - now) <= timedelta(minutes=121)
    assert SelfCareModule().evaluate(store, _ctx(now)) == []  # deferred by the confirm
    # …but it's never "done", and it fires again once the interval elapses.
    status = self_care_status(store, now, "UTC")
    bio = {c["key"]: c for c in status["checks"]}["biobreak"]
    assert bio["open_ended"] is True and bio["done"] is False
    later = now + timedelta(minutes=125)
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(later)), "biobreak")


# -- wind-down: an evening once-a-day check, off by default (like meds) ------


def _winddown_only(store):
    """Enable self-care with only the wind-down check on (it's opt-in)."""
    store.set_state("self_care", "on")
    for other in ("meal", "water", "meds", "biobreak"):
        store.set_state(f"{other}_enabled", "off")
    store.set_state("winddown_enabled", "on")


def test_winddown_off_by_default_but_opt_in_fires(store):
    """Wind-down stays silent even with self_care on until winddown_enabled is set."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    at_21 = _ctx(datetime(2026, 7, 2, 21, 0, 0), name="Tom")
    assert SelfCareModule().evaluate(store, at_21) == []  # off by default
    store.set_state("winddown_enabled", "on")
    cues = SelfCareModule().evaluate(store, at_21)
    assert [c.context_key for c in cues] == ["winddown"]
    assert cues[0].intervention == "winddown_check"
    assert "wind" in cues[0].text.lower() and "bed" in cues[0].text.lower()


def test_winddown_silent_before_its_evening_start(store):
    """Nothing in the afternoon — the wind-down window is the evening (default 21:00)."""
    _winddown_only(store)
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0))) == []
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 21, 30, 0))), "winddown")


def test_winddown_silent_once_confirmed(store):
    """One 'Winding down' settles it for the night (target 1), re-arming next day."""
    _winddown_only(store)
    store.set_state("winddown_count", "2026-07-02|1")  # target 1 — done for the night
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 21, 30, 0))) == []
    # A new day re-arms it (the count is date-scoped).
    assert _by_kind(
        SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 3, 21, 30, 0))), "winddown"
    )


def test_winddown_message_greets_by_name():
    from prefrontal.modules.self_care import winddown_message
    assert winddown_message("Tom").startswith("Tom, wind-down check")
    assert winddown_message().startswith("Wind-down check")


def test_winddown_status_has_no_end_hour(store):
    """Like meal/meds it's bounded by its target + responsive hours, not a wall clock."""
    status = self_care_status(store, datetime(2026, 7, 2, 21, 0, 0), "UTC")
    by_key = {c["key"]: c for c in status["checks"]}
    assert by_key["winddown"]["end_hour"] is None
    assert by_key["winddown"]["open_ended"] is False


def test_act_winddown_started_counts_logs_and_settles(client, store):
    """A 'Winding down' tap counts toward target 1, logs an episode, done for the night."""
    token = sign_action(DEFAULT_HANDLE, "winddown_started", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("winddown_count") == f"{today}|1"  # target 1 met
    eps = store.episodes_by_type("self_care")
    assert eps and eps[0]["outcome"] == "confirmed" and "winddown" in eps[0]["context"]


def test_winddown_relies_on_quiet_hours_not_an_end_hour(store):
    """The design contract: the engine's quiet-hours gate — not a bedtime in the
    module — is what keeps wind-down from nagging into the night.

    The module keeps emitting the cue while past its start hour and unmet; it's
    ``suppressed`` that drops it once outside responsive hours."""
    from prefrontal.coaching import suppressed
    _winddown_only(store)
    # Responsive hours 8–22: at 23:00 the module still *emits* a cue…
    late = _ctx(datetime(2026, 7, 2, 23, 0, 0))
    cue = _by_kind(SelfCareModule().evaluate(store, late), "winddown")
    assert cue is not None
    # …but the engine suppresses it (a non-critical cue outside responsive hours).
    assert suppressed(store, cue, late) is True
    # Inside responsive hours (21:30) the same cue is allowed through.
    ok = _ctx(datetime(2026, 7, 2, 21, 30, 0))
    early = _by_kind(SelfCareModule().evaluate(store, ok), "winddown")
    assert suppressed(store, early, ok) is False


# -- movement / stretch: an evening once-a-day floor, off by default (like meds) --


def _movement_only(store):
    """Enable self-care with only the movement check on (it's opt-in)."""
    store.set_state("self_care", "on")
    for other in ("meal", "water", "meds", "biobreak"):
        store.set_state(f"{other}_enabled", "off")
    store.set_state("movement_enabled", "on")


def test_movement_off_by_default_but_opt_in_fires(store):
    """Movement stays silent even with self_care on until movement_enabled is set."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("water_enabled", "off")
    store.set_state("biobreak_enabled", "off")
    at_20 = _ctx(datetime(2026, 7, 2, 20, 0, 0), name="Tom")
    assert SelfCareModule().evaluate(store, at_20) == []  # off by default
    store.set_state("movement_enabled", "on")
    cues = SelfCareModule().evaluate(store, at_20)
    assert [c.context_key for c in cues] == ["movement"]
    assert cues[0].intervention == "movement_check"
    assert "move" in cues[0].text.lower() and "stretch" in cues[0].text.lower()


def test_movement_silent_before_its_evening_start(store):
    """Nothing in the afternoon — the movement window defaults to the evening (20:00)."""
    _movement_only(store)
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0))) == []
    evening = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 20, 30, 0)))
    assert _by_kind(evening, "movement")


def test_movement_start_hour_is_configurable(store):
    """The anchor is a one-setting change — a morning person can move it to 07:00."""
    _movement_only(store)
    store.set_state("movement_start_hour", "7")
    morning = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 7, 30, 0)))
    assert _by_kind(morning, "movement")


def test_movement_silent_once_confirmed(store):
    """One 'Stretched' settles it for the day (target 1), re-arming next day."""
    _movement_only(store)
    store.set_state("movement_count", "2026-07-02|1")  # target 1 — done for the day
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 20, 30, 0))) == []
    # A new day re-arms it (the count is date-scoped).
    assert _by_kind(
        SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 3, 20, 30, 0))), "movement"
    )


def test_movement_message_greets_by_name():
    from prefrontal.modules.self_care import movement_message
    assert movement_message("Tom").startswith("Tom, movement check")
    assert movement_message().startswith("Movement check")


def test_movement_status_has_no_end_hour(store):
    """Like meal/meds/winddown it's bounded by its target + responsive hours, not a wall clock."""
    status = self_care_status(store, datetime(2026, 7, 2, 20, 0, 0), "UTC")
    by_key = {c["key"]: c for c in status["checks"]}
    assert by_key["movement"]["end_hour"] is None
    assert by_key["movement"]["open_ended"] is False


def test_act_movement_stretched_counts_logs_and_settles(client, store):
    """A 'Stretched' tap counts toward target 1, logs an episode, done for the day."""
    token = sign_action(DEFAULT_HANDLE, "movement_stretched", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("movement_count") == f"{today}|1"  # target 1 met
    eps = store.episodes_by_type("self_care")
    assert eps and eps[0]["outcome"] == "confirmed" and "movement" in eps[0]["context"]


# -- one-tap actions ---------------------------------------------------------


def test_self_care_actions_in_allowlist():
    for action in ("meal_ate", "meal_snooze", "water_drank", "water_snooze",
                   "meds_took", "meds_snooze", "winddown_started", "winddown_snooze",
                   "movement_stretched", "movement_snooze"):
        token = sign_action(DEFAULT_HANDLE, action, 20260702, SIGNING)
        assert verify_action(token, SIGNING) == (DEFAULT_HANDLE, action, 20260702)


def test_act_meal_ate_counts_and_logs(client, store):
    token = sign_action(DEFAULT_HANDLE, "meal_ate", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("meal_count") == f"{today}|1"  # counted toward target 1
    # The response is logged as a self_care episode (seeds cadence learning).
    eps = store.episodes_by_type("self_care")
    assert eps and eps[0]["outcome"] == "confirmed" and "meal" in eps[0]["context"]


def test_act_meal_snooze_sets_window(client, store):
    token = sign_action(DEFAULT_HANDLE, "meal_snooze", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    until = store.get_state(SNOOZED_UNTIL_KEY)
    assert until is not None
    parsed = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=25) < (parsed - utcnow()) <= timedelta(minutes=31)


def test_act_water_drank_counts_and_defers_a_full_interval(client, store):
    """A 'Drank' tap counts one and pushes the next reminder ~a full interval out."""
    token = sign_action(DEFAULT_HANDLE, "water_drank", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("water_count") == f"{today}|1"  # one of the daily target
    parsed = datetime.strptime(store.get_state("water_snoozed_until"), "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=85) < (parsed - utcnow()) <= timedelta(minutes=91)


def test_act_water_snooze_is_shorter(client, store):
    token = sign_action(DEFAULT_HANDLE, "water_snooze", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    parsed = datetime.strptime(store.get_state("water_snoozed_until"), "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=25) < (parsed - utcnow()) <= timedelta(minutes=31)


# -- coach/check delivery ----------------------------------------------------


def _always_on(store):
    store.set_state("self_care", "on")
    store.set_state("meal_start_hour", "0")  # any time of day qualifies
    store.set_state("water_start_hour", "0")
    store.set_state("responsive_hours_start", "0")  # always responsive (no quiet-hours skip)
    store.set_state("responsive_hours_end", "0")


def test_coach_check_surfaces_meal_cue_with_actions(client, store):
    """The tick returns the meal cue with one-tap Ate / Snooze buttons."""
    _always_on(store)
    store.set_state("water_enabled", "off")
    cues = client.post("/webhooks/coach/check", json={}, headers=_auth()).json()["cues"]
    meal = next(c for c in cues if c["context_key"] == "meal")
    assert meal["module"] == "self_care"
    assert [a["label"] for a in meal["actions"]] == ["✓ Ate", "Snooze"]
    token = meal["actions"][0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING)[1] == "meal_ate"


def test_coach_check_surfaces_water_cue_with_actions(client, store):
    """The tick returns the water cue with one-tap Drank / Snooze buttons."""
    _always_on(store)
    store.set_state("meal_enabled", "off")
    cues = client.post("/webhooks/coach/check", json={}, headers=_auth()).json()["cues"]
    water = next(c for c in cues if c["context_key"] == "water")
    assert [a["label"] for a in water["actions"]] == ["✓ Drank", "Snooze"]
    token = water["actions"][0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING)[1] == "water_drank"


# -- adaptive cadence: latency capture + honesty check (learning §6) ----------


def _meal_decision():
    cue = Cue(
        module="self_care", intervention="meal_check", urgency="nudge",
        text="eat?", context_key="meal", dedup_key="m", ref={"target": 20260703},
    )
    return Decision(cue=cue, channel="push", text="eat?")


def _responses(confirmed=0, snoozed=0, latency=None):
    out = [{"outcome": "confirmed", "latency": latency} for _ in range(confirmed)]
    out += [{"outcome": "snoozed", "latency": None} for _ in range(snoozed)]
    return out


def test_latency_captured_from_prompt_to_tap(store):
    """mark_self_care_prompted + a later confirm records the nudge→tap latency."""
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    mark_self_care_prompted(store, [_meal_decision()], t0)
    apply_self_care_action(store, "meal_ate", now=t0 + timedelta(seconds=90), today="2026-07-03")
    ep = store.episodes_by_type(SELF_CARE_EPISODE)[0]
    assert "latency=90s" in ep["notes"]


def test_latency_unknown_without_a_prompt(store):
    """A tap with no tracked prompt logs latency=? rather than a bogus number."""
    apply_self_care_action(
        store, "water_drank", now=datetime(2026, 7, 3, 12, 0, 0), today="2026-07-03"
    )
    assert "latency=?" in store.episodes_by_type(SELF_CARE_EPISODE)[0]["notes"]


def test_adapt_widens_when_snoozed_often():
    suggested, reason = adapt_self_care_interval(
        _responses(confirmed=2, snoozed=3), 40, current_interval=40
    )
    assert suggested == 50 and "snooze" in reason  # 40 × 1.25 → 50


def test_adapt_holds_on_reflexive_instant_confirms():
    """The honesty check: instant yeses must NOT be read as 'needs it less'."""
    suggested, reason = adapt_self_care_interval(
        _responses(confirmed=5, latency=5.0), 40, current_interval=40  # all instant
    )
    assert suggested == 40 and "dismissal" in reason


def test_adapt_eases_off_on_genuine_engagement():
    suggested, reason = adapt_self_care_interval(
        _responses(confirmed=5, latency=200.0), 40, current_interval=40  # plausible gaps
    )
    assert suggested == 45 and "easing off" in reason  # 40 × 1.1 → 45


def test_adapt_insufficient_data_holds():
    suggested, reason = adapt_self_care_interval(_responses(confirmed=2), 40, current_interval=40)
    assert suggested == 40 and "not enough" in reason


def test_adapt_widens_when_ignored_often():
    """An unanswered (ignored) nudge is a 'not now' signal too — it widens."""
    responses = [{"outcome": "ignored", "latency": None} for _ in range(3)]
    responses += [{"outcome": "confirmed", "latency": 200.0} for _ in range(2)]
    suggested, reason = adapt_self_care_interval(responses, 40, current_interval=40)
    assert suggested == 50 and "ignore" in reason  # 3/5 resisted → widen 40×1.25


def test_further_widen_held_until_it_helps():
    """Once widened, a further widen must be earned — if push-back hasn't eased, hold."""
    # Already at 50 (> default 40). Recent half resists as much as the older half,
    # so the last widen didn't help → hold rather than compound it.
    recent = [{"outcome": "snoozed", "latency": None} for _ in range(5)]
    older = [{"outcome": "snoozed", "latency": None} for _ in range(3)]
    older += [{"outcome": "confirmed", "latency": 200.0} for _ in range(2)]
    suggested, reason = adapt_self_care_interval(recent + older, 40, current_interval=50)
    assert suggested == 50 and "holding to see it help" in reason


def test_further_widen_allowed_once_pushback_eases():
    """If the recent half resists less than the older, the widen earned its keep."""
    recent = [{"outcome": "confirmed", "latency": 200.0} for _ in range(4)]
    recent += [{"outcome": "snoozed", "latency": None}]
    older = [{"outcome": "snoozed", "latency": None} for _ in range(5)]
    suggested, reason = adapt_self_care_interval(recent + older, 40, current_interval=50)
    assert suggested == 60 and "less frequently" in reason  # 50 × 1.25 → 60


def test_sweep_logs_ignored_and_clears_the_stamp(store):
    """A self-care nudge un-acted past the window becomes an 'ignored' episode."""
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    mark_self_care_prompted(store, [_meal_decision()], t0)
    # Well within the window → nothing swept yet.
    assert sweep_unanswered_self_care(store, t0 + timedelta(minutes=5)) == 0
    # Past the window → one ignored episode, and the stamp is cleared.
    assert sweep_unanswered_self_care(store, t0 + timedelta(minutes=40)) == 1
    ep = store.episodes_by_type(SELF_CARE_EPISODE)[0]
    assert ep["outcome"] == "ignored" and ep["context"] == "meal: ignored"
    # Idempotent: the stamp is gone, so a second sweep finds nothing.
    assert sweep_unanswered_self_care(store, t0 + timedelta(minutes=80)) == 0


def test_sweep_skips_an_answered_nudge(store):
    """A nudge that got a tap clears its stamp, so the sweep never flags it."""
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    mark_self_care_prompted(store, [_meal_decision()], t0)
    apply_self_care_action(store, "meal_ate", now=t0 + timedelta(minutes=2), today="2026-07-03")
    assert sweep_unanswered_self_care(store, t0 + timedelta(minutes=40)) == 0


def _log_response(store, kind, outcome):
    store.log_episode(SELF_CARE_EPISODE, context=f"{kind}: {outcome}", outcome=outcome, notes="30m")


def test_adapt_self_care_writes_key_but_respects_explicit(store):
    store.set_state("self_care", "on")
    for _ in range(4):
        _log_response(store, "meal", "snoozed")
    _log_response(store, "meal", "confirmed")

    summary = adapt_self_care(store)
    meal = next(c for c in summary if c["check"] == "meal")
    assert meal["changed"] and store.get_state("meal_reask_minutes") == "50"

    # A user-pinned interval is reported but never overwritten.
    store.set_state("water_interval_minutes", "120", source="explicit")
    for _ in range(5):
        _log_response(store, "water", "snoozed")
    water = next(c for c in adapt_self_care(store) if c["check"] == "water")
    assert not water["changed"] and store.get_state("water_interval_minutes") == "120"


# -- self_care_status (dashboard card / GET /self-care) ----------------------


def test_status_reports_off_master_switch(store):
    """The whole point of the surface: a silently-off switch is visible as off."""
    status = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    assert status["enabled"] is False
    # Even off, the per-check shape is present so the card can render it.
    keys = {c["key"] for c in status["checks"]}
    assert keys == {"meal", "water", "meds", "biobreak", "winddown", "movement"}


def test_status_reports_progress_and_meds_disabled(store):
    store.set_state("self_care", "on")
    store.set_state("meal_count", "2026-07-02|1")   # meal target is 1 → done
    store.set_state("water_count", "2026-07-02|3")  # 3 of 6
    status = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    assert status["enabled"] is True
    by_key = {c["key"]: c for c in status["checks"]}
    assert by_key["meal"]["count"] == 1 and by_key["meal"]["done"] is True
    assert by_key["water"]["count"] == 3 and by_key["water"]["target"] == 6
    assert by_key["water"]["done"] is False
    # Meds ships disabled even under the master switch (medication is personal).
    assert by_key["meds"]["enabled"] is False
    assert by_key["meal"]["start_hour"] == 11 and by_key["water"]["start_hour"] == 9


def test_status_overdue_flag(store):
    """A check is overdue when enabled, unmet, and past its start hour — the flash cue."""
    store.set_state("self_care", "on")
    store.set_state("meds_enabled", "on")  # meds ship off; turn on for this user
    # Meds start hour is 9. At 15:00 with nothing logged → overdue.
    late = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    assert next(c for c in late["checks"] if c["key"] == "meds")["overdue"] is True
    # Before the start hour (08:00) → not overdue yet.
    early = self_care_status(store, datetime(2026, 7, 2, 8, 0, 0), "UTC")
    assert next(c for c in early["checks"] if c["key"] == "meds")["overdue"] is False
    # Once taken, it's not overdue even later in the day.
    store.set_state("meds_count", "2026-07-02|1")
    taken = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    assert next(c for c in taken["checks"] if c["key"] == "meds")["overdue"] is False
    # A disabled check is never overdue (meal left on by the master switch here).
    store.set_state("meds_enabled", "off")
    off = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    assert next(c for c in off["checks"] if c["key"] == "meds")["overdue"] is False


def test_status_overdue_recurring_is_pace_aware(store):
    """Water (recurring) is overdue only when behind the expected per-interval pace,
    not merely because it hasn't hit the daily total yet."""
    store.set_state("self_care", "on")
    # Water: start 9:00, interval 90m, target 6. By 12:00 that's 3h = 2 intervals,
    # so ~2 expected. With 0 logged → behind → overdue.
    behind = self_care_status(store, datetime(2026, 7, 2, 12, 0, 0), "UTC")
    assert next(c for c in behind["checks"] if c["key"] == "water")["overdue"] is True
    # Right after the start hour, before a full interval has passed → not yet behind.
    fresh = self_care_status(store, datetime(2026, 7, 2, 9, 30, 0), "UTC")
    assert next(c for c in fresh["checks"] if c["key"] == "water")["overdue"] is False
    # Caught up to pace (2 logged by 12:00) → not overdue.
    store.set_state("water_count", "2026-07-02|2")
    onpace = self_care_status(store, datetime(2026, 7, 2, 12, 0, 0), "UTC")
    assert next(c for c in onpace["checks"] if c["key"] == "water")["overdue"] is False


def test_status_start_hour_tolerates_hh_mm(store):
    """A start hour hand-edited to "HH:MM" still reports the right hour."""
    store.set_state("self_care", "on")
    store.set_state("meal_start_hour", "12:30", source="explicit")
    status = self_care_status(store, datetime(2026, 7, 2, 15, 0, 0), "UTC")
    meal = next(c for c in status["checks"] if c["key"] == "meal")
    assert meal["start_hour"] == 12


def test_self_care_endpoint_requires_auth(client):
    assert client.get("/self-care").status_code == 401


def test_self_care_endpoint_returns_status(client, store):
    store.set_state("self_care", "on")
    store.set_state("water_count", f"{utcnow().strftime('%Y-%m-%d')}|2")
    body = client.get("/self-care", headers=_auth()).json()
    assert body["enabled"] is True
    water = next(c for c in body["checks"] if c["key"] == "water")
    assert water["count"] == 2 and water["target"] == 6


# -- apply_self_care_config + POST /self-care --------------------------------


def test_apply_config_master_and_per_check_fields(store):
    apply_self_care_config(
        store,
        enabled=True,
        checks={
            "meal": {"enabled": False},
            "water": {"target": 8, "start_hour": 7, "interval_minutes": 60},
            "meds": {"enabled": True},
        },
    )
    assert store.get_state("self_care") == "on"
    assert store.get_state("meal_enabled") == "off"
    assert store.get_state("water_daily_target") == "8"
    assert store.get_state("water_start_hour") == "7"
    assert store.get_state("water_interval_minutes") == "60"
    assert store.get_state("meds_enabled") == "on"
    # Writes are explicit, so the nightly learner leaves them alone.
    assert store.all_state()["water_interval_minutes"]["source"] == "explicit"


def test_apply_config_is_partial_and_clamps(store):
    store.set_state("self_care", "on")
    apply_self_care_config(store, checks={"water": {"target": 0, "start_hour": 30}})
    assert store.get_state("self_care") == "on"        # untouched (not in payload)
    assert store.get_state("water_daily_target") == "1"  # clamped up to >= 1
    assert store.get_state("water_start_hour") == "23"   # clamped into 0..23


def test_apply_config_ignores_unknown_check(store):
    apply_self_care_config(store, checks={"nope": {"enabled": True}})
    assert store.get_state("nope_enabled") is None


def test_self_care_post_requires_auth(client):
    assert client.post("/self-care", json={"enabled": True}).status_code == 401


def test_self_care_post_updates_and_returns_status(client, store):
    body = {"enabled": True, "checks": {"water": {"target": 5}, "meds": {"enabled": True}}}
    resp = client.post("/self-care", json=body, headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    by_key = {c["key"]: c for c in data["checks"]}
    assert by_key["water"]["target"] == 5
    assert by_key["meds"]["enabled"] is True
    # And it persisted.
    assert store.get_state("water_daily_target") == "5"


def test_self_care_post_rejects_out_of_range(client):
    # Pydantic bounds (start_hour 0..23, target/interval >= 1) → 422, no write.
    r = client.post("/self-care", json={"checks": {"water": {"start_hour": 99}}}, headers=_auth())
    assert r.status_code == 422


# -- apply_self_care_mark + POST /self-care/mark -----------------------------
# The dashboard card's "mark what I did today" path: a signed-in user records a
# confirm by check key, with the same count/episode semantics as a notification
# tap — but no signed one-tap token (they're already authenticated).


def test_apply_mark_counts_and_logs_like_a_tap(store):
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    headline = apply_self_care_mark(store, "meal", now=t0, today="2026-07-03")
    assert headline  # confirmation copy for the caller to flash
    assert store.get_state("meal_count") == "2026-07-03|1"
    eps = store.episodes_by_type("self_care")
    assert eps and eps[0]["outcome"] == "confirmed" and "meal" in eps[0]["context"]


def test_apply_mark_unknown_key_is_none(store):
    assert apply_self_care_mark(store, "nope", now=utcnow(), today="2026-07-03") is None


def test_self_care_mark_requires_auth(client):
    assert client.post("/self-care/mark", json={"key": "meal"}).status_code == 401


def test_self_care_mark_endpoint_counts_and_returns_status(client, store):
    resp = client.post("/self-care/mark", json={"key": "meal"}, headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["headline"]
    today = utcnow().strftime("%Y-%m-%d")
    assert store.get_state("meal_count") == f"{today}|1"
    # Fresh status rides along so the card re-renders: meal now reads done.
    by_key = {c["key"]: c for c in data["checks"]}
    assert by_key["meal"]["count"] == 1 and by_key["meal"]["done"] is True


def test_self_care_mark_unknown_key_404(client):
    assert client.post("/self-care/mark", json={"key": "nope"}, headers=_auth()).status_code == 404


# -- apply_self_care_unmark + POST /self-care/mark {undo: true} --------------
# The chip's shift-click mis-tap correction: rewind today's count by one, never
# below zero, touching only the day cursor (episodes are an immutable log).


def test_apply_unmark_decrements_and_floors_at_zero(store):
    today = "2026-07-03"
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    for _ in range(2):
        apply_self_care_mark(store, "water", now=t0, today=today)
    assert store.get_state("water_count") == f"{today}|2"
    assert apply_self_care_unmark(store, "water", today=today)  # confirmation copy
    assert store.get_state("water_count") == f"{today}|1"
    apply_self_care_unmark(store, "water", today=today)
    assert store.get_state("water_count") == f"{today}|0"
    apply_self_care_unmark(store, "water", today=today)  # already zero — stays put
    assert store.get_state("water_count") == f"{today}|0"


def test_apply_unmark_leaves_the_episode_log_intact(store):
    today = "2026-07-03"
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    apply_self_care_mark(store, "meal", now=t0, today=today)
    apply_self_care_unmark(store, "meal", today=today)
    # The count is rewound, but the confirm we logged stays in history.
    assert store.get_state("meal_count") == f"{today}|0"
    eps = store.episodes_by_type("self_care")
    assert len(eps) == 1 and eps[0]["outcome"] == "confirmed"


def test_apply_unmark_unknown_key_is_none(store):
    assert apply_self_care_unmark(store, "nope", today="2026-07-03") is None


def test_self_care_mark_undo_endpoint_flips_done_off(client, store):
    today = utcnow().strftime("%Y-%m-%d")
    client.post("/self-care/mark", json={"key": "meal"}, headers=_auth())
    resp = client.post("/self-care/mark", json={"key": "meal", "undo": True}, headers=_auth())
    assert resp.status_code == 200
    assert store.get_state("meal_count") == f"{today}|0"
    by_key = {c["key"]: c for c in resp.json()["checks"]}
    assert by_key["meal"]["count"] == 0 and by_key["meal"]["done"] is False


# -- apply_self_care_reset + POST /self-care/mark {reset: true} --------------
# The chip's tap-at-max wrap-around: once a check hits its target, the next plain
# tap cycles the count back to zero (mobile has no shift-click). Like unmark it
# rewinds only the day cursor; the episode log is left intact.


def test_apply_reset_zeroes_the_day_count(store):
    today = "2026-07-03"
    t0 = datetime(2026, 7, 3, 12, 0, 0)
    for _ in range(3):
        apply_self_care_mark(store, "water", now=t0, today=today)
    assert store.get_state("water_count") == f"{today}|3"
    headline = apply_self_care_reset(store, "water", today=today)
    assert headline  # confirmation copy
    assert store.get_state("water_count") == f"{today}|0"
    # The confirms we logged stay in history (an append-only log).
    assert len(store.episodes_by_type("self_care")) == 3


def test_apply_reset_unknown_key_is_none(store):
    assert apply_self_care_reset(store, "nope", today="2026-07-03") is None


def test_self_care_mark_reset_endpoint_wraps_to_zero(client, store):
    """A recurring check at its target: reset cycles it back to 0 and not-done."""
    today = utcnow().strftime("%Y-%m-%d")
    store.set_state("water_count", f"{today}|6")  # at the default target of 6
    resp = client.post("/self-care/mark", json={"key": "water", "reset": True}, headers=_auth())
    assert resp.status_code == 200
    assert store.get_state("water_count") == f"{today}|0"
    by_key = {c["key"]: c for c in resp.json()["checks"]}
    assert by_key["water"]["count"] == 0 and by_key["water"]["done"] is False


def test_self_care_mark_reset_takes_precedence_over_undo(client, store):
    """reset zeroes the count even if undo is also set (documented precedence)."""
    today = utcnow().strftime("%Y-%m-%d")
    store.set_state("meal_count", f"{today}|1")
    resp = client.post(
        "/self-care/mark", json={"key": "meal", "undo": True, "reset": True}, headers=_auth()
    )
    assert resp.status_code == 200
    assert store.get_state("meal_count") == f"{today}|0"
