"""Tests for departure planning — when to leave for an upcoming commitment.

Covers the pure travel-estimate / leave-by / level functions, the last-known
location store helpers and its idempotent column migration, and the
``/webhooks/location`` + ``/webhooks/departure/check`` HTTP surface (including
fire-once dedup and the distance-vs-lead fallback).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.departure import (
    attribute_departure,
    build_departure_message,
    classify_departure,
    departure_level,
    departure_mode,
    estimate_travel_minutes,
    evaluate_departure_check,
    next_departure,
    plan_departure,
    record_departure_outcome,
)
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.migrate import backfill_added_columns
from prefrontal.memory.repos.nudges import DEFAULT_NUDGE_TTL_HOURS
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "departure-secret"
NOW = datetime(2026, 6, 29, 12, 0, 0)


def _commit(
    start: str,
    *,
    dest=None,
    lead=10.0,
    title="Dentist",
    location=None,
    cid=1,
    calendar_key=None,
):
    """Build a commitment dict for the pure functions."""
    c = {
        "id": cid,
        "title": title,
        "start_at": start,
        "lead_minutes": lead,
        "location": location,
        "dest_lat": dest[0] if dest else None,
        "dest_lon": dest[1] if dest else None,
        "calendar_key": calendar_key,
    }
    return c


def _at(base: datetime, minutes: float) -> str:
    """A UTC timestamp string ``minutes`` after ``base`` (for the pure tests)."""
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


# -- pure functions ----------------------------------------------------------


def test_estimate_travel_minutes_known_distance():
    """5 km at 30 km/h with no road factor is 10 minutes; road factor scales it."""
    assert estimate_travel_minutes(5000, speed_kmh=30, road_factor=1.0) == pytest.approx(10.0)
    assert estimate_travel_minutes(5000, speed_kmh=30, road_factor=1.5) == pytest.approx(15.0)


def test_estimate_travel_minutes_zero_speed_is_safe():
    """A non-positive speed degrades to 0 rather than dividing by zero."""
    assert estimate_travel_minutes(5000, speed_kmh=0) == 0.0


def test_departure_level_thresholds():
    """Level escalates as the leave-by time approaches and passes."""
    assert departure_level(45, 30, 10) == "none"
    assert departure_level(25, 30, 10) == "heads_up"
    assert departure_level(8, 30, 10) == "soon"
    assert departure_level(0, 30, 10) == "go"
    assert departure_level(-12, 30, 10) == "go"


def test_plan_departure_uses_distance_when_coords_known():
    """With current + destination coords, travel time drives the leave-by."""
    # Commitment 8 min out, destination == current location (zero travel), prep 5.
    plan = plan_departure(
        _commit("2026-06-29 12:08:00", dest=(0.0, 0.0)),
        current_lat=0.0,
        current_lon=0.0,
        prep_minutes=5.0,
        now=NOW,
    )
    assert plan.basis == "distance"
    assert plan.travel_minutes == 0.0
    # leave_by = 12:08 - 5 (prep) = 12:03, so ~3 min from noon -> "soon".
    assert plan.leave_by == "2026-06-29 12:03:00"
    assert plan.minutes_until_leave == pytest.approx(3.0)
    assert plan.level == "soon"


def test_plan_departure_applies_bias_to_travel():
    """The time bias pads the travel estimate (pushing leave-by earlier)."""
    base = plan_departure(
        _commit("2026-06-29 13:00:00", dest=(0.1, 0.0)),
        current_lat=0.0,
        current_lon=0.0,
        bias=1.0,
        prep_minutes=0.0,
        now=NOW,
    )
    biased = plan_departure(
        _commit("2026-06-29 13:00:00", dest=(0.1, 0.0)),
        current_lat=0.0,
        current_lon=0.0,
        bias=2.0,
        prep_minutes=0.0,
        now=NOW,
    )
    assert biased.travel_minutes == pytest.approx(base.travel_minutes * 2, rel=1e-6)


def test_plan_departure_falls_back_to_lead_without_coords():
    """No destination coords -> use the commitment's static lead_minutes."""
    plan = plan_departure(
        _commit("2026-06-29 12:04:00", lead=10.0),  # no dest
        current_lat=0.0,
        current_lon=0.0,
        now=NOW,
    )
    assert plan.basis == "lead"
    assert plan.travel_minutes is None
    # leave_by = 12:04 - 10 = 11:54, already 6 min past -> "go".
    assert plan.minutes_until_leave == pytest.approx(-6.0)
    assert plan.level == "go"


def test_plan_departure_falls_back_when_location_unknown():
    """Destination coords but no current location still falls back to lead."""
    plan = plan_departure(
        _commit("2026-06-29 12:40:00", dest=(0.0, 0.0), lead=10.0),
        current_lat=None,
        current_lon=None,
        now=NOW,
    )
    assert plan.basis == "lead"


def test_next_departure_picks_most_urgent():
    """The soonest-to-leave reminder-worthy plan wins; far-off ones are skipped."""
    soon = plan_departure(_commit("2026-06-29 12:08:00", lead=5.0, cid=1), now=NOW)
    far = plan_departure(_commit("2026-06-29 18:00:00", lead=5.0, cid=2), now=NOW)
    assert next_departure([far, soon]).commitment["id"] == 1
    # Nothing reminder-worthy -> None.
    assert next_departure([far]) is None


def test_build_departure_message_per_level():
    """Each level produces a distinct, sensible message."""
    soon = plan_departure(
        _commit("2026-06-29 12:08:00", lead=5.0, location="123 Main St"), now=NOW
    )
    assert "get ready" in build_departure_message(soon)
    assert "123 Main St" in build_departure_message(soon)

    late = plan_departure(_commit("2026-06-29 11:58:00", lead=5.0), now=NOW)
    msg = build_departure_message(late, name="Tom")
    assert msg.startswith("Hey Tom,")
    assert "past your leave time" in msg


# -- attend mode: "I'm already where I need to be" ---------------------------


def test_departure_mode_defaults_work_to_attend_others_to_travel():
    """Work-calendar commitments default to attend; everything else travels."""
    assert departure_mode(_commit(_at(NOW, 10), calendar_key="work")) == "attend"
    assert departure_mode(_commit(_at(NOW, 10), calendar_key="personal")) == "travel"
    assert departure_mode(_commit(_at(NOW, 10), calendar_key=None)) == "travel"


def test_departure_mode_commute_tag_forces_travel():
    """A [commute] tag on a work meeting flips just that one back to travel."""
    tagged = _commit(_at(NOW, 10), calendar_key="work", title="Board mtg [commute]")
    assert departure_mode(tagged) == "travel"
    at_loc = _commit(_at(NOW, 10), calendar_key="work", location="HQ [onsite]")
    assert departure_mode(at_loc) == "travel"


def test_departure_mode_office_day_forces_travel():
    """Flagging an in-office day makes every work commitment travel again."""
    c = _commit(_at(NOW, 10), calendar_key="work")
    assert departure_mode(c, office_day=True) == "travel"


def test_plan_departure_attend_mode_ignores_travel():
    """A work commitment uses a fixed lead and ignores destination coords."""
    # Start in 3 min; dest is far away and location is known — but attend mode
    # ignores both. Default work lead is 5 min, so leave_by is already past.
    c = _commit("2026-06-29 12:03:00", dest=(40.0, 40.0), calendar_key="work", cid=9)
    plan = plan_departure(c, current_lat=0.0, current_lon=0.0, now=NOW)
    assert plan.mode == "attend"
    assert plan.basis == "attend"
    assert plan.travel_minutes is None
    assert plan.leave_by == "2026-06-29 11:58:00"  # 12:03 - 5 min lead
    assert plan.level == "go"


def test_plan_departure_attend_mode_is_a_single_reminder_not_a_ladder():
    """Attend mode stays quiet until the short lead, then fires one reminder."""
    # 20 min out: a travel item would be "heads_up"; attend says nothing yet.
    far = plan_departure(_commit(_at(NOW, 20), calendar_key="work", cid=9), now=NOW)
    assert far.level == "none"
    # 4 min out (inside the 5-min lead): the single reminder fires.
    near = plan_departure(_commit(_at(NOW, 4), calendar_key="work", cid=9), now=NOW)
    assert near.level == "go"


def test_plan_departure_office_day_restores_travel_path():
    """With office_day set, a work commitment plans travel from current coords."""
    c = _commit("2026-06-29 12:08:00", dest=(0.0, 0.0), calendar_key="work", cid=9)
    plan = plan_departure(
        c, current_lat=0.0, current_lon=0.0, prep_minutes=5.0, office_day=True, now=NOW
    )
    assert plan.mode == "travel"
    assert plan.basis == "distance"


def test_build_departure_message_attend_mode_never_says_leave():
    """Attend copy reassures the user rather than telling them to leave."""
    c = _commit(_at(NOW, 4), calendar_key="work", title="Standup", location="the office", cid=9)
    plan = plan_departure(c, now=NOW)
    msg = build_departure_message(plan, name="Tom")
    assert msg.startswith("Hey Tom,")
    assert "Standup" in msg and "the office" in msg
    assert "already where you need to be" in msg
    assert "leave" not in msg.lower()


# -- store: last-known location + migration ----------------------------------


def test_location_round_trip():
    """set_location then get_location returns the position with a timestamp."""
    conn = init_db(":memory:")
    try:
        store = scoped_default(MemoryStore(conn))
        assert store.get_location() is None
        store.set_location(37.77, -122.41, accuracy_m=12.5)
        loc = store.get_location()
        assert loc["lat"] == pytest.approx(37.77)
        assert loc["lon"] == pytest.approx(-122.41)
        assert loc["accuracy_m"] == pytest.approx(12.5)
        assert loc["at"]  # a timestamp was recorded
    finally:
        conn.close()


def test_migrate_adds_dest_columns_idempotently():
    """backfill_added_columns adds dest_lat/dest_lon on a pre-existing table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE commitments (id INTEGER PRIMARY KEY, title TEXT)")
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(commitments)")}
    assert {"dest_lat", "dest_lon"} <= cols
    backfill_added_columns(conn)  # second run is a no-op, not an error
    conn.close()


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _utc(delta_minutes: float) -> str:
    return (utcnow() + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_location_ping_requires_auth(client):
    assert client.post("/webhooks/location", json={"lat": 1, "lon": 2}).status_code == 401


def test_location_ping_stores_and_reads_back(client):
    resp = client.post(
        "/webhooks/location", json={"lat": 37.77, "lon": -122.41}, headers=_auth()
    )
    assert resp.status_code == 200 and resp.json()["stored"] is True
    got = client.get("/location", headers=_auth()).json()["location"]
    assert got["lat"] == pytest.approx(37.77)


def test_outing_check_uses_stored_location_for_gating(client, store):
    """A stored location within the home radius closes the outing with no body."""
    store.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
    store.set_location(0.0, 0.0)  # right at home
    resp = client.post("/webhooks/outing/check", json={}, headers=_auth())
    item = resp.json()["active"][0]
    assert item["at_home"] is True
    assert item["status"] == "returned"


def test_departure_check_fires_once_per_level(client, store):
    """A due departure fires once; re-polling the same level does not re-fire."""
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Dentist",
        start_at=_utc(8),  # 8 min out
        dest_lat=0.0,
        dest_lon=0.0,  # zero travel; prep(5) -> leave ~3 min -> "soon"
        location="123 Main St",
        source="manual",
    )
    first = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert first["fire"] is True
    assert first["reminder"]["level"] == "soon"
    assert first["reminder"]["basis"] == "distance"
    assert "get ready" in first["message"]
    assert first["location_known"] is True

    again = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert again["fire"] is False  # same (commitment, level) -> deduped
    assert again["reminder"]["level"] == "soon"


def test_evaluate_departure_check_is_usable_without_http(store):
    """The decision runs on a store directly — no request, no router (§10)."""
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Dentist", start_at=_utc(8), dest_lat=0.0, dest_lon=0.0,
        location="123 Main St", source="manual",
    )

    first = evaluate_departure_check(store, name="Sam")
    assert first.fire is True
    assert first.reminder is not None
    assert first.reminder["level"] == "soon"
    assert first.location_known is True
    assert "Sam" in first.message
    # The router-only decoration is NOT added by the domain function.
    assert "dismiss_url" not in first.reminder

    # Same (commitment, level) on the next tick → deduped, no re-fire.
    again = evaluate_departure_check(store, name="Sam")
    assert again.fire is False
    assert again.reminder["level"] == "soon"


def test_evaluate_departure_check_falls_back_to_stored_location(store):
    """With no coords passed, it reads the last-known location from the store."""
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Dentist", start_at=_utc(8), dest_lat=0.0, dest_lon=0.0, source="manual",
    )
    result = evaluate_departure_check(store)  # no current_lat/lon supplied
    assert result.location_known is True
    assert result.reminder is not None


def test_departure_check_records_nudge_and_lists_it(client, store):
    """A fired departure nudge is recorded once and surfaces via GET /nudges."""
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Dentist", start_at=_utc(8), dest_lat=0.0, dest_lon=0.0,
        location="123 Main St", source="manual",
    )
    fired = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert fired["fire"] is True

    listed = client.get("/nudges", headers=_auth()).json()["nudges"]
    assert len(listed) == 1
    assert listed[0]["kind"] == "departure"
    assert listed[0]["level"] == "soon"
    assert listed[0]["message"] == fired["message"]
    # The nudge expires at the meeting's start, so it stops surfacing once the
    # meeting has begun rather than lingering on the widget for hours.
    assert listed[0]["expires_at"] == fired["reminder"]["start_at"]

    # A no-fire re-poll (same level) logs no duplicate nudge.
    client.post("/webhooks/departure/check", json={}, headers=_auth())
    assert len(client.get("/nudges", headers=_auth()).json()["nudges"]) == 1


def test_recent_nudges_hides_expired(store):
    """recent_nudges omits a nudge whose expires_at has passed; a fresh one stays."""
    store.record_nudge(kind="departure", message="stale", level="go", expires_at=_utc(-90))
    store.record_nudge(kind="departure", message="live", level="go", expires_at=_utc(20))
    store.record_nudge(kind="outing", message="fresh-outing", level="soft")

    messages = [n["message"] for n in store.recent_nudges()]
    assert "stale" not in messages  # its meeting started 90 min ago
    assert {"live", "fresh-outing"} <= set(messages)


def test_record_nudge_defaults_expiry_when_none_given(store):
    """An outing nudge (no natural end) gets a default TTL so it can't linger.

    Without this, an outing "still on track?" nudge stayed NULL-expiry and
    lingered on the widget for hours after you were back.
    """
    store.record_nudge(kind="outing", message="on track?", level="soft")
    row = store.recent_nudges()[0]
    assert row["expires_at"] is not None
    expiry = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    expected = utcnow() + timedelta(hours=DEFAULT_NUDGE_TTL_HOURS)
    assert abs((expiry - expected).total_seconds()) < 120  # ~2h out, within tolerance


def test_record_nudge_preserves_explicit_expiry(store):
    """An explicit expires_at (e.g. a departure's start_at) is not overridden."""
    start = _utc(15)
    store.record_nudge(kind="departure", message="leave now", level="go", expires_at=start)
    assert store.recent_nudges()[0]["expires_at"] == start


def test_close_outing_expires_its_nudge(store):
    """Closing an outing clears its "still on track?" nudge immediately.

    The default TTL bounds a nudge to a couple hours; closing the outing it was
    about should retire it the moment you're back, not hours later. A departure
    nudge (different kind) is untouched.
    """
    outing_id = store.start_outing("getting coffee", 15.0)
    store.record_nudge(kind="outing", message="still on track?", level="soft")
    store.record_nudge(kind="departure", message="leave now", level="go", expires_at=_utc(20))
    assert {"still on track?", "leave now"} <= {n["message"] for n in store.recent_nudges()}

    store.close_outing(outing_id, status="returned")

    messages = {n["message"] for n in store.recent_nudges()}
    assert "still on track?" not in messages  # cleared on return
    assert "leave now" in messages  # a departure nudge is left alone


def test_close_outing_when_not_active_leaves_nudges(store):
    """Re-closing an already-closed outing doesn't touch live outing nudges."""
    outing_id = store.start_outing("getting coffee", 15.0)
    store.close_outing(outing_id, status="returned")
    store.record_nudge(kind="outing", message="fresh after return", level="soft")

    store.close_outing(outing_id, status="returned")  # no active outing to close
    assert "fresh after return" in {n["message"] for n in store.recent_nudges()}


def test_migrate_adds_nudges_expires_at_idempotently():
    """backfill_added_columns adds expires_at on a pre-existing nudges table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nudges (id INTEGER PRIMARY KEY, message TEXT)")
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nudges)")}
    assert "expires_at" in cols
    backfill_added_columns(conn)  # second run is a no-op, not an error
    conn.close()


def test_nudges_endpoint_empty_ordering_and_limit(client, store):
    """GET /nudges is empty by default, newest-first, and honors the limit param."""
    assert client.get("/nudges", headers=_auth()).json()["nudges"] == []
    for i in range(3):
        store.record_nudge(kind="outing", message=f"m{i}", level="soft")
    listed = client.get("/nudges", headers=_auth()).json()["nudges"]
    assert [n["message"] for n in listed] == ["m2", "m1", "m0"]  # newest first
    assert len(client.get("/nudges?limit=2", headers=_auth()).json()["nudges"]) == 2


def test_departure_check_attend_mode_for_work_meeting(client, store):
    """A work meeting you're not at gives a 5-min heads-up, not a 'leave now'."""
    store.set_location(0.0, 0.0)  # nowhere near the office
    store.upsert_commitment(
        title="Team sync",
        start_at=_utc(4),  # 4 min out, inside the 5-min work lead
        external_id="work:1",  # calendar_key -> "work" -> attend mode
        dest_lat=40.0,
        dest_lon=40.0,  # office coords, deliberately far away
        location="HQ",
        source="calendar",
    )
    resp = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert resp["fire"] is True
    assert resp["reminder"]["mode"] == "attend"
    assert resp["reminder"]["basis"] == "attend"
    assert resp["reminder"]["travel_minutes"] is None
    assert "already where you need to be" in resp["message"]
    assert "leave" not in resp["message"].lower()


def test_departure_check_office_day_toggle_restores_travel(client, store):
    """Flagging an in-office day switches work meetings back to the leave-now nudge."""
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Team sync",
        start_at=_utc(4),
        external_id="work:1",
        dest_lat=0.0,
        dest_lon=0.0,  # zero travel; travel path -> basis "distance"
        location="HQ",
        source="calendar",
    )
    on = client.post(
        "/webhooks/departure/office-day", json={}, headers=_auth()
    ).json()
    assert on["office_day"] is True and on["until"]
    travel = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert travel["reminder"]["mode"] == "travel"
    assert travel["reminder"]["basis"] == "distance"

    off = client.post(
        "/webhooks/departure/office-day", json={"on": False}, headers=_auth()
    ).json()
    assert off["office_day"] is False
    attend = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert attend["reminder"]["mode"] == "attend"


def test_departure_check_falls_back_to_lead_without_location(client, store):
    """With no known location, the reminder uses lead_minutes (basis='lead')."""
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), lead_minutes=10.0, source="manual"
    )
    resp = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert resp["location_known"] is False
    assert resp["reminder"]["basis"] == "lead"
    assert resp["fire"] is True


def test_calendar_sync_persists_destination_coords(client):
    """dest_lat/dest_lon survive a calendar sync and surface in /commitments."""
    client.post(
        "/webhooks/calendar/sync",
        json={
            "events": [
                {
                    "title": "Dentist",
                    "start_at": _utc(120),
                    "external_id": "cal:1",
                    "dest_lat": 37.7,
                    "dest_lon": -122.4,
                }
            ]
        },
        headers=_auth(),
    )
    commitments = client.get("/commitments", headers=_auth()).json()["commitments"]
    assert commitments[0]["dest_lat"] == pytest.approx(37.7)
    assert commitments[0]["dest_lon"] == pytest.approx(-122.4)


# -- Departure outcome capture (did you actually leave on time?) -------------


def test_classify_departure_on_time_late_and_early():
    """On time within grace, late past it, and early counts as time to spare."""
    plan = plan_departure(_commit(_at(NOW, 30), lead=10.0), now=NOW)  # leave_by = NOW+20
    assert classify_departure(plan, NOW + timedelta(minutes=20)) == ("success", 0.0)
    assert classify_departure(plan, NOW + timedelta(minutes=25)) == ("miss", 5.0)
    out, late = classify_departure(plan, NOW + timedelta(minutes=15))
    assert out == "success" and late == -5.0


def test_attribute_departure_picks_soonest_and_skips_unrelated():
    """A departure attributes to the soonest in-window commitment, else None."""
    a = plan_departure(_commit(_at(NOW, 30), lead=10.0, title="A", cid=1), now=NOW)
    b = plan_departure(_commit(_at(NOW, 180), lead=10.0, title="B", cid=2), now=NOW)
    # In both windows → soonest (A) wins.
    picked = attribute_departure([a, b], NOW + timedelta(minutes=55))
    assert picked is not None and picked.commitment["id"] == 1
    # Only B's window → B.
    picked_b = attribute_departure([a, b], NOW + timedelta(minutes=120))
    assert picked_b is not None and picked_b.commitment["id"] == 2
    # Long before anything → unattributable.
    assert attribute_departure([a, b], NOW - timedelta(hours=4)) is None


def test_record_departure_outcome_logs_a_departure_episode(store):
    """A late departure logs a `departure` miss with actual_value left None."""
    plan = plan_departure(_commit(_at(NOW, 30), lead=10.0), now=NOW)  # leave_by NOW+20
    rec = record_departure_outcome(store, plan, NOW + timedelta(minutes=32))
    assert rec["outcome"] == "miss" and rec["lateness_minutes"] == 12.0
    ep = store.get_episode(rec["episode_id"])
    assert ep["episode_type"] == "departure"
    assert ep["outcome"] == "miss"
    assert ep["actual_value"] is None  # never pollutes the shared time bias
    assert ep["predicted_value"] == 10.0  # planned buffer (informational)
    assert "late" in ep["notes"]


def test_departure_left_records_and_is_idempotent(client, store):
    """POST records the outcome once; a second (chatty geofence) call no-ops."""
    store.upsert_commitment(
        title="Dentist", start_at=_utc(20), lead_minutes=10.0, source="manual"
    )  # leave_by ~now+10 → leaving now is ~10 min early → success
    first = client.post("/webhooks/departure/left", json={}, headers=_auth()).json()
    assert first["recorded"] is True
    assert first["outcome"] == "success"
    assert "on time" in first["confirmation"]
    again = client.post("/webhooks/departure/left", json={}, headers=_auth()).json()
    assert again["recorded"] is False and again["reason"] == "already_recorded"


def test_departure_left_late_departure_is_a_miss(client, store):
    """When the leave-by has already passed, leaving now is logged as a miss."""
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), lead_minutes=10.0, source="manual"
    )  # leave_by ~now-5 → already past
    resp = client.post("/webhooks/departure/left", json={}, headers=_auth()).json()
    assert resp["recorded"] is True and resp["outcome"] == "miss"
    assert "late" in resp["confirmation"]


def test_departure_left_no_matching_commitment(client):
    """With nothing on the calendar, a departure isn't attributed (no noise)."""
    resp = client.post("/webhooks/departure/left", json={}, headers=_auth()).json()
    assert resp["recorded"] is False and resp["reason"] == "no_matching_commitment"


def test_departure_left_skipped_when_module_disabled(store):
    """Disabling Time Blindness turns off automatic departure capture."""
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET, modules=("hyperfocus",)),
    )
    store.upsert_commitment(
        title="Dentist", start_at=_utc(20), lead_minutes=10.0, source="manual"
    )
    with TestClient(app) as c:
        resp = c.post("/webhooks/departure/left", json={}, headers=_auth()).json()
    assert resp["recorded"] is False and resp["skipped"] == "module_disabled"
