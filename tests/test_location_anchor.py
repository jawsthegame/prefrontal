"""Tests for the Location-Aware Task Anchor module.

Covers the pure escalation logic and time-window parser, the outings store
methods, and the /webhooks/outing/{start,check,return} endpoints (including that
each escalation level fires exactly once).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.location_anchor import (
    build_message,
    escalation_level,
    haversine_m,
    parse_time_window,
)
from prefrontal.webhooks.app import create_app

SECRET = "anchor-secret"


def _utc_minutes_ago(minutes: float) -> str:
    """A SQLite-style UTC timestamp `minutes` in the past (for departure_at)."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


# -- pure logic --------------------------------------------------------------


@pytest.mark.parametrize(
    ("elapsed", "window", "expected"),
    [
        (0, 15, "none"),
        (7, 15, "none"),     # 47% < 50%
        (7.5, 15, "soft"),   # exactly 50%
        (10, 15, "soft"),
        (15, 15, "firm"),    # exactly 100%
        (20, 15, "firm"),
        (22.5, 15, "call"),  # exactly 150%
        (30, 15, "call"),
        (10, 0, "none"),     # no window -> nothing to escalate
    ],
)
def test_escalation_level(elapsed, window, expected):
    """Thresholds land at 50/100/150% of the stated window."""
    assert escalation_level(elapsed, window) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Going to get coffee, back in 15 minutes", 15.0),
        ("15 min", 15.0),
        ("back in 1.5 hours", 90.0),
        ("an hour", 60.0),
        ("half an hour", 30.0),
        ("just running out", None),
    ],
)
def test_parse_time_window(text, expected):
    """The parser extracts a window from natural language, or returns None."""
    assert parse_time_window(text) == expected


def test_build_message_uses_name_for_call():
    """The voice-call message includes the name when provided."""
    msg = build_message("call", elapsed_minutes=22, window_minutes=15, name="Tom")
    assert "Tom" in msg
    assert "22 minutes" in msg
    # The soft message has no name baked in when name is empty.
    assert build_message("soft", elapsed_minutes=7, window_minutes=15).startswith("Hey,")


def test_haversine_known_distance():
    """Roughly one degree of latitude is ~111 km."""
    d = haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_000


# -- store -------------------------------------------------------------------


def test_outing_store_lifecycle():
    """Start -> active (with elapsed) -> close (with actual minutes)."""
    with MemoryStore.open(":memory:") as store:
        oid = store.start_outing(
            "getting coffee", 15.0, departure_at=_utc_minutes_ago(8)
        )
        active = store.active_outings()
        assert len(active) == 1
        assert active[0]["id"] == oid
        assert active[0]["elapsed_minutes"] == pytest.approx(8, abs=0.5)

        closed = store.close_outing(oid)
        assert closed["status"] == "returned"
        assert closed["actual_minutes"] == pytest.approx(8, abs=0.5)
        # No longer active.
        assert store.active_outings() == []
        # Double-close is a no-op.
        assert store.close_outing(oid) is None


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store():
    """An in-memory store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    """A TestClient wired to the injected store with auth enabled."""
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_start_parses_window_from_intention(client):
    """Starting without an explicit window parses it from the text."""
    resp = client.post(
        "/webhooks/outing/start",
        json={"intention": "Going to get coffee, back in 15 minutes"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    assert resp.json()["time_window_minutes"] == 15.0


def test_start_requires_a_window(client):
    """A non-parseable intention with no explicit window is a 422."""
    resp = client.post(
        "/webhooks/outing/start",
        json={"intention": "just heading out"},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_check_fires_each_level_once(client, store):
    """Crossing a threshold fires once; the next poll at the same level does not."""
    # 8 minutes into a 15-minute window -> soft (53%).
    store.start_outing("getting coffee", 15.0, departure_at=_utc_minutes_ago(8))

    first = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()
    assert first["active"][0]["level"] == "soft"
    assert first["active"][0]["fire"] is True
    assert "still on track" in first["active"][0]["message"]

    # Same level on the next poll -> no re-fire.
    second = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()
    assert second["active"][0]["fire"] is False
    assert second["active"][0]["message"] == ""


def test_check_reports_call_level_and_distance(client, store):
    """Past 150% returns level 'call'; coords yield a distance annotation."""
    store.start_outing(
        "getting coffee",
        10.0,
        home_lat=0.0,
        home_lon=0.0,
        departure_at=_utc_minutes_ago(16),  # 160% -> call
    )
    resp = client.post(
        "/webhooks/outing/check",
        json={"current_lat": 0.05, "current_lon": 0.0},
        headers=_auth(),
    ).json()
    item = resp["active"][0]
    assert item["level"] == "call"
    assert item["fire"] is True
    assert item["distance_m"] is not None and item["distance_m"] > 0


def test_return_logs_episode_with_outcome(client, store):
    """Returning over the window logs a 'miss' task episode."""
    store.start_outing("getting coffee", 10.0, departure_at=_utc_minutes_ago(18))
    resp = client.post("/webhooks/outing/return", json={}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "miss"
    assert body["actual_minutes"] == pytest.approx(18, abs=0.5)
    # The episode is queryable for pattern tracking.
    ep = store.get_episode(body["episode_id"])
    assert ep["episode_type"] == "task"
    assert ep["predicted_value"] == 10.0
    assert "coffee" in ep["context"]


def test_return_without_active_outing_is_404(client):
    """Returning with nothing active is a 404."""
    assert client.post("/webhooks/outing/return", json={}, headers=_auth()).status_code == 404


def test_outing_endpoints_require_auth(client):
    """All anchor endpoints are token-guarded."""
    assert client.post("/webhooks/outing/start", json={"intention": "x"}).status_code == 401
    assert client.post("/webhooks/outing/check", json={}).status_code == 401
    assert client.post("/webhooks/outing/return", json={}).status_code == 401
