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
    mark_self_care_prompted,
    meal_message,
    sweep_unanswered_self_care,
    water_message,
)
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.oauth import sign_action, verify_action
from tests.conftest import DEFAULT_HANDLE, scoped_default

SECRET = "self-care-secret"
SIGNING = "self-care-signing"
BASE = "https://agent-1.tail8b0a.ts.net"


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


def test_both_checks_fire_when_enabled(store):
    """With the master on, meal + water both fire in their windows."""
    store.set_state("self_care", "on")
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0), name="Tom"))
    assert {c.context_key for c in cues} == {"meal", "water"}


def test_meds_off_by_default_but_opt_in_fires(store):
    """Meds stays silent even with self_care on until meds_enabled is set."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    store.set_state("water_enabled", "off")
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
    # 10:00 — after water's 9:00 start but before the meal's 11:00.
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 10, 0, 0)))
    assert [c.context_key for c in cues] == ["water"]
    assert cues[0].intervention == "water_check"


def test_meal_silent_before_start_hour(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    # 09:00 is before the default 11:00 meal start.
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 9, 0, 0))) == []


def test_meal_silent_once_target_met(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    store.set_state("meal_count", "2026-07-02|1")  # target is 1 — done for the day
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0))) == []
    # A new day re-arms it (the count is date-scoped).
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 3, 15, 0, 0))), "meal")


def test_water_stops_after_daily_target(store):
    """Water keeps firing until the daily target of confirms is reached."""
    store.set_state("self_care", "on")
    store.set_state("meal_enabled", "off")
    now = _ctx(datetime(2026, 7, 2, 10, 0, 0))
    store.set_state("water_count", "2026-07-02|5")  # one short of the default 6
    assert _by_kind(SelfCareModule().evaluate(store, now), "water")
    store.set_state("water_count", "2026-07-02|6")  # target met
    assert SelfCareModule().evaluate(store, now) == []


def test_meal_silent_while_snoozed(store):
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    now = datetime(2026, 7, 2, 15, 0, 0)
    store.set_state(SNOOZED_UNTIL_KEY, (now + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"))
    assert SelfCareModule().evaluate(store, _ctx(now)) == []
    # Past the snooze it fires again.
    assert _by_kind(SelfCareModule().evaluate(store, _ctx(now + timedelta(minutes=25))), "meal")


def test_reask_bucket_advances_dedup_key(store):
    """Same re-ask window → same dedup_key; a later window → a new one."""
    store.set_state("self_care", "on")
    store.set_state("water_enabled", "off")
    mod = SelfCareModule()
    k1 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0)))[0].dedup_key
    k2 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 20, 0)))[0].dedup_key
    k3 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 45, 0)))[0].dedup_key
    assert k1 == k2  # within the same 40-min window
    assert k3 != k1  # a later window


# -- one-tap actions ---------------------------------------------------------


def test_self_care_actions_in_allowlist():
    for action in ("meal_ate", "meal_snooze", "water_drank", "water_snooze",
                   "meds_took", "meds_snooze"):
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
