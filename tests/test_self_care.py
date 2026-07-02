"""Tests for the self-care module — the "have you eaten?" meal check.

Covers the cue producer (:class:`~prefrontal.modules.self_care.SelfCareModule`)
on the coaching tick, the one-tap **Ate** / **Snooze** actions via
``GET /nudge/act``, and that ``/webhooks/coach/check`` surfaces the meal cue with
its signed action buttons.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.coaching import CoachContext
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import (
    ATE_TODAY_KEY,
    SNOOZED_UNTIL_KEY,
    SelfCareModule,
    meal_message,
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


# -- module.evaluate ---------------------------------------------------------


def test_meal_message_greets_by_name():
    assert meal_message("Tom").startswith("Tom, quick check")
    assert meal_message().startswith("Quick check")


def test_off_by_default(store):
    """No cue unless the self_care key is explicitly on."""
    ctx = _ctx(datetime(2026, 7, 2, 15, 0, 0))
    assert SelfCareModule().evaluate(store, ctx) == []


def test_fires_when_due(store):
    store.set_state("self_care", "on")
    cues = SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0), name="Tom"))
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "self_care"
    assert cue.intervention == "meal_check"
    assert cue.urgency == "nudge"
    assert cue.context_key == "meal"
    assert "eaten" in cue.text.lower()
    assert cue.ref["target"] == 20260702


def test_silent_before_start_hour(store):
    store.set_state("self_care", "on")
    # 09:00 is before the default 11:00 start.
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 9, 0, 0))) == []


def test_silent_once_eaten_today(store):
    store.set_state("self_care", "on")
    store.set_state(ATE_TODAY_KEY, "2026-07-02")
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0))) == []
    # A new day re-arms it.
    assert SelfCareModule().evaluate(store, _ctx(datetime(2026, 7, 3, 15, 0, 0)))


def test_silent_while_snoozed(store):
    store.set_state("self_care", "on")
    now = datetime(2026, 7, 2, 15, 0, 0)
    store.set_state(SNOOZED_UNTIL_KEY, (now + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"))
    assert SelfCareModule().evaluate(store, _ctx(now)) == []
    # Past the snooze it fires again.
    assert SelfCareModule().evaluate(store, _ctx(now + timedelta(minutes=25)))


def test_reask_bucket_advances_dedup_key(store):
    """Same re-ask window → same dedup_key; a later window → a new one."""
    store.set_state("self_care", "on")
    mod = SelfCareModule()
    k1 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 0, 0)))[0].dedup_key
    k2 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 20, 0)))[0].dedup_key
    k3 = mod.evaluate(store, _ctx(datetime(2026, 7, 2, 15, 45, 0)))[0].dedup_key
    assert k1 == k2  # within the same 40-min window
    assert k3 != k1  # a later window


# -- one-tap actions ---------------------------------------------------------


def test_meal_actions_in_allowlist():
    for action in ("meal_ate", "meal_snooze"):
        token = sign_action(DEFAULT_HANDLE, action, 20260702, SIGNING)
        assert verify_action(token, SIGNING) == (DEFAULT_HANDLE, action, 20260702)


def test_act_meal_ate_marks_eaten(client, store):
    token = sign_action(DEFAULT_HANDLE, "meal_ate", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    assert store.get_state(ATE_TODAY_KEY) == utcnow().strftime("%Y-%m-%d")


def test_act_meal_snooze_sets_window(client, store):
    token = sign_action(DEFAULT_HANDLE, "meal_snooze", 20260702, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    until = store.get_state(SNOOZED_UNTIL_KEY)
    assert until is not None
    parsed = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    assert timedelta(minutes=25) < (parsed - utcnow()) <= timedelta(minutes=31)


# -- coach/check delivery ----------------------------------------------------


def test_coach_check_surfaces_meal_cue_with_actions(client, store):
    """The tick returns the meal cue with one-tap Ate / Snooze buttons."""
    store.set_state("self_care", "on")
    store.set_state("meal_start_hour", "0")  # any time of day qualifies
    store.set_state("responsive_hours_start", "0")  # always responsive (no quiet-hours skip)
    store.set_state("responsive_hours_end", "0")
    cues = client.post("/webhooks/coach/check", json={}, headers=_auth()).json()["cues"]
    meal = next(c for c in cues if c["module"] == "self_care")
    assert meal["context_key"] == "meal"
    assert [a["label"] for a in meal["actions"]] == ["✓ Ate", "Snooze"]
    token = meal["actions"][0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING)[1] == "meal_ate"
