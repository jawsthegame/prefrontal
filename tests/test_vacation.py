"""Tests for vacation mode (:mod:`prefrontal.vacation`).

Covers the pure state core (activate / deactivate / status / resume-on-return),
the coaching suppression gate it drives, the location state machine's auto-resume
on returning home, the HTTP surface (``GET`` / ``POST /vacation`` and the
``/webhooks/location`` echo), and the ``prefrontal vacation`` CLI.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.cli import main
from prefrontal.coaching import CoachContext, Cue, build_context, suppressed
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.registry import get as get_module
from prefrontal.nudges import apply_nudge_action
from prefrontal.trips import process_location
from prefrontal.vacation import (
    activate,
    deactivate,
    is_on_vacation,
    resume_on_return,
    should_suggest_vacation,
    suggest_threshold_minutes,
    vacation_status,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default


def _minutes_ago(minutes: float) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

HOME = (40.0000, -73.0000)
NEAR = (40.0004, -73.0004)   # ~55 m — inside the default 150 m radius
FAR = (40.0500, -73.0500)    # ~7 km — well outside

NOON = datetime(2026, 7, 2, 12, 0, 0)  # inside the default 8–22 responsive window
SECRET = "vacation-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _cue(urgency="nudge", **kw) -> Cue:
    return Cue(
        module="m", intervention="i", urgency=urgency, text="hi",
        context_key="todo", dedup_key="k", **kw,
    )


# -- pure state core ---------------------------------------------------------


def test_off_by_default(store):
    assert is_on_vacation(store) is False
    assert vacation_status(store) == {"active": False, "since": None, "source": None}


def test_activate_then_status(store):
    status = activate(store, now=NOON, source="manual")
    assert status["active"] is True
    assert status["source"] == "manual"
    assert status["since"] == "2026-07-02 12:00:00"
    assert is_on_vacation(store) is True


def test_reactivate_preserves_since(store):
    """A second toggle refreshes the source but keeps the original start time."""
    activate(store, now=NOON, source="manual")
    later = datetime(2026, 7, 5, 9, 0, 0)
    status = activate(store, now=later, source="auto")
    assert status["since"] == "2026-07-02 12:00:00"  # unchanged
    assert status["source"] == "auto"


def test_deactivate_is_clean_slate(store):
    activate(store, now=NOON)
    status = deactivate(store)
    assert status == {"active": False, "since": None, "source": None}
    # No stale keys left to leak into a later display.
    assert store.get_state("vacation_since") is None
    assert store.get_state("vacation_source") is None


def test_deactivate_when_off_is_noop(store):
    assert deactivate(store)["active"] is False


def test_resume_on_return_lifts_active_vacation(store):
    activate(store, now=NOON)
    assert resume_on_return(store) is True
    assert is_on_vacation(store) is False


def test_resume_on_return_noop_when_off(store):
    assert resume_on_return(store) is False


# -- coaching suppression gate -----------------------------------------------


def test_vacation_holds_discretionary_cues_but_not_critical(store):
    activate(store, now=NOON)
    ctx = build_context(store, now=NOON)
    assert ctx.on_vacation is True
    assert suppressed(store, _cue("nudge"), ctx) is True
    assert suppressed(store, _cue("urgent"), ctx) is True
    # A hard, time-sensitive obligation (a flight) still gets through.
    assert suppressed(store, _cue("critical"), ctx) is False
    # As does a cue the user themselves started.
    assert suppressed(store, _cue("nudge", user_initiated=True), ctx) is False


def test_vacation_overrides_quiet_hours_exempt(store):
    """An evening-recap cue that would skip quiet hours is still held on vacation."""
    activate(store, now=NOON)
    ctx = build_context(store, now=NOON)
    assert suppressed(store, _cue("nudge", quiet_hours_exempt=True), ctx) is True


def test_no_suppression_when_off(store):
    ctx = build_context(store, now=NOON)
    assert ctx.on_vacation is False
    assert suppressed(store, _cue("nudge"), ctx) is False


# -- location state machine: auto-resume on return ---------------------------


def test_return_home_lifts_vacation(store):
    """Leaving on vacation, then returning inside the home radius, clears it."""
    store.set_home(*HOME)
    activate(store, now=NOON, source="manual")

    depart = process_location(store, *FAR)
    assert depart["event"] == "depart"
    assert depart["vacation_resumed"] is False  # still out, still on vacation
    assert is_on_vacation(store) is True

    ret = process_location(store, *NEAR)
    assert ret["event"] == "return"
    assert ret["vacation_resumed"] is True
    assert is_on_vacation(store) is False


def test_return_without_vacation_reports_false(store):
    store.set_home(*HOME)
    process_location(store, *FAR)
    ret = process_location(store, *NEAR)
    assert ret["event"] == "return"
    assert ret["vacation_resumed"] is False


def test_staycation_survives_pings_near_home(store):
    """A manual vacation with no departure is never auto-lifted (no return edge)."""
    store.set_home(*HOME)
    activate(store, now=NOON)
    # Pings that never leave the radius produce no trip and no return edge.
    assert process_location(store, *NEAR)["event"] is None
    assert is_on_vacation(store) is True


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_vacation_endpoints_roundtrip(client):
    assert client.get("/vacation", headers=_auth()).json()["active"] is False

    on = client.post("/vacation", json={"active": True}, headers=_auth()).json()
    assert on["active"] is True
    assert on["source"] == "manual"
    assert on["since"] is not None

    assert client.get("/vacation", headers=_auth()).json()["active"] is True

    off = client.post("/vacation", json={"active": False}, headers=_auth()).json()
    assert off == {"active": False, "since": None, "source": None}


def test_location_endpoint_echoes_vacation_resume(client):
    """Returning home over the webhook lifts vacation and echoes it to the client."""
    assert client.post("/webhooks/home", json={"lat": HOME[0], "lon": HOME[1]},
                       headers=_auth()).status_code == 201
    assert client.post("/vacation", json={"active": True},
                       headers=_auth()).json()["active"] is True

    client.post("/webhooks/location", json={"lat": FAR[0], "lon": FAR[1]}, headers=_auth())
    ret = client.post(
        "/webhooks/location", json={"lat": NEAR[0], "lon": NEAR[1]}, headers=_auth()
    ).json()
    assert ret["trip"]["event"] == "return"
    assert ret["vacation_resumed"] is True
    assert client.get("/vacation", headers=_auth()).json()["active"] is False


# -- entry suggestion: pure logic --------------------------------------------


def test_suggest_threshold_default_override_and_floor(store):
    assert suggest_threshold_minutes(store) == 2 * 1440
    store.set_state("vacation_suggest_after_nights", "3")
    assert suggest_threshold_minutes(store) == 3 * 1440
    store.set_state("vacation_suggest_after_nights", "0")  # floored to one night
    assert suggest_threshold_minutes(store) == 1440


def test_should_suggest_vacation_logic(store):
    # Below the 2-night threshold → no.
    assert should_suggest_vacation(store, away_minutes=1440, already_asked=False) is False
    # At/over the threshold → yes.
    assert should_suggest_vacation(store, away_minutes=2 * 1440, already_asked=False) is True
    # Already asked this absence → no re-nag.
    assert should_suggest_vacation(store, away_minutes=5 * 1440, already_asked=True) is False
    # Already on vacation → nothing to suggest.
    activate(store, now=NOON)
    assert should_suggest_vacation(store, away_minutes=5 * 1440, already_asked=False) is False


# -- entry suggestion: the trip_tracking cue ---------------------------------


def _vacation_cues(store):
    module = get_module("trip_tracking")
    ctx = CoachContext(now=datetime.utcnow())
    return [c for c in module.evaluate(store, ctx) if c.context_key == "vacation_suggest"]


def test_module_suggests_vacation_after_multiday_absence(store):
    store.set_home(*HOME)
    store.open_trip(departed_at=_minutes_ago(3 * 1440))  # away 3 days
    cues = _vacation_cues(store)
    assert len(cues) == 1
    assert cues[0].ref["trip_id"] == store.active_trip()["id"]
    assert cues[0].intervention == "vacation_suggest"
    assert "🏝️" in cues[0].text


def test_module_no_suggest_for_short_trip(store):
    store.set_home(*HOME)
    store.open_trip(departed_at=_minutes_ago(60))  # an hour out
    assert _vacation_cues(store) == []


def test_module_no_suggest_when_already_on_vacation(store):
    store.set_home(*HOME)
    store.open_trip(departed_at=_minutes_ago(3 * 1440))
    activate(store, now=NOON)
    assert _vacation_cues(store) == []


def test_module_suggests_once_per_absence(store):
    """Once the ask has fired for a trip, the fire-once guard silences re-asks."""
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(3 * 1440))
    assert len(_vacation_cues(store)) == 1
    # The engine stamps coach_fired:<dedup_key> when the cue fires.
    store.set_state(f"coach_fired:vacation_suggest:{trip_id}", _minutes_ago(1))
    assert _vacation_cues(store) == []


# -- entry suggestion: the one-tap confirm -----------------------------------


def test_vacation_confirm_turns_on_source_auto(store):
    store.set_home(*HOME)
    process_location(store, *FAR)  # open a trip → out
    trip_id = store.active_trip()["id"]
    user = {"id": 1, "handle": "tester", "display_name": "Tester"}
    headline = apply_nudge_action(
        store, "vacation_confirm", trip_id, user=user, settings=Settings()
    )
    assert "Vacation mode on" in headline
    assert is_on_vacation(store) is True
    assert vacation_status(store)["source"] == "auto"


def test_vacation_confirm_when_home_is_noop(store):
    """A stale tap after returning home doesn't mute the assistant."""
    store.set_home(*HOME)  # no open trip
    user = {"id": 1, "handle": "tester", "display_name": "Tester"}
    headline = apply_nudge_action(
        store, "vacation_confirm", 999, user=user, settings=Settings()
    )
    assert "back home" in headline
    assert is_on_vacation(store) is False


def test_vacation_confirm_ignores_stale_target(store):
    """A tap carrying an earlier trip's id doesn't mute during a later trip."""
    store.set_home(*HOME)
    process_location(store, *FAR)  # a new (different) trip is active now
    current = store.active_trip()["id"]
    user = {"id": 1, "handle": "tester", "display_name": "Tester"}
    headline = apply_nudge_action(
        store, "vacation_confirm", current + 999, user=user, settings=Settings()
    )
    assert "earlier trip" in headline
    assert is_on_vacation(store) is False


# -- CLI ---------------------------------------------------------------------


def test_cli_vacation_roundtrip(tmp_path, capsys):
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tom", "--operator"]) == 0
    capsys.readouterr()

    base = ["vacation", "--db-path", str(db), "--user", "tom"]

    assert main(base) == 0  # default: status
    assert "OFF" in capsys.readouterr().out

    assert main([*base, "on"]) == 0
    assert "ON" in capsys.readouterr().out

    assert main([*base, "status"]) == 0
    assert "ON" in capsys.readouterr().out

    assert main([*base, "off"]) == 0
    assert "OFF" in capsys.readouterr().out
