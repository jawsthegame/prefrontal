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
    build_departure_message,
    departure_level,
    estimate_travel_minutes,
    next_departure,
    plan_departure,
)
from prefrontal.impact import utcnow
from prefrontal.memory.db import _migrate, init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "departure-secret"
NOW = datetime(2026, 6, 29, 12, 0, 0)


def _commit(start: str, *, dest=None, lead=10.0, title="Dentist", location=None, cid=1):
    """Build a commitment dict for the pure functions."""
    c = {
        "id": cid,
        "title": title,
        "start_at": start,
        "lead_minutes": lead,
        "location": location,
        "dest_lat": dest[0] if dest else None,
        "dest_lon": dest[1] if dest else None,
    }
    return c


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
    """_migrate back-fills dest_lat/dest_lon on a pre-existing table, repeatably."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE commitments (id INTEGER PRIMARY KEY, title TEXT)")
    _migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(commitments)")}
    assert {"dest_lat", "dest_lon"} <= cols
    _migrate(conn)  # second run is a no-op, not an error
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
