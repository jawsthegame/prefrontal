"""Tests for impact analysis — what gets thrown off when running behind.

Covers the pure projection/analysis/phrase functions and the integration into
the outing check endpoint (at-risk commitment surfaced + named in the nudge).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.impact import (
    analyze_impact,
    at_risk,
    impact_phrase,
    project_free_time,
    utcnow,
)
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "impact-secret"


def _utc(delta_minutes: float) -> str:
    """Stored-format UTC timestamp `delta_minutes` from now."""
    return (utcnow() + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _commit(title, start_delta, lead=10.0, hardness="soft"):
    return {
        "id": 1,
        "title": title,
        "start_at": _utc(start_delta),
        "lead_minutes": lead,
        "hardness": hardness,
    }


# -- pure functions ----------------------------------------------------------


def test_project_free_time_applies_bias():
    """Realistic free-time = departure + window * bias."""
    dep = "2026-06-28 10:00:00"
    # 15-min window, 1.4 bias -> 21 min -> 10:21. Use now far in the past so the
    # projection (not now) wins.
    proj = project_free_time(dep, 15.0, 1.4, now=datetime(2026, 6, 28, 9, 0, 0))
    assert proj == datetime(2026, 6, 28, 10, 21, 0)


def test_project_free_time_floors_at_now():
    """If the realistic finish is already past, you could leave now."""
    dep = "2026-06-28 10:00:00"
    now = datetime(2026, 6, 28, 12, 0, 0)
    assert project_free_time(dep, 15.0, 1.4, now=now) == now


def test_analyze_impact_flags_at_risk_and_orders():
    """Commitments you can't reach in time are flagged, worst-first."""
    free = utcnow()
    commitments = [
        _commit("Safe", 120),       # plenty of slack
        _commit("Tight", 5),        # start in 5m, 10m lead -> already too late
        _commit("Doomed", 2),       # even worse
    ]
    impacts = analyze_impact(free, commitments)
    risky = at_risk(impacts)
    titles = [i.commitment["title"] for i in risky]
    assert "Safe" not in titles
    assert titles[0] == "Doomed"  # most negative slack first
    assert set(titles) == {"Doomed", "Tight"}


def test_impact_phrase_names_top_risk():
    """The phrase names the most-threatened commitment, or is empty."""
    free = utcnow()
    risky = at_risk(analyze_impact(free, [_commit("Team sync", 2)]))
    phrase = impact_phrase(risky)
    assert "Team sync" in phrase and "at risk" in phrase
    assert impact_phrase([]) == ""


# -- endpoint integration ----------------------------------------------------


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


def test_check_surfaces_impact_and_names_it_in_message(client, store):
    """An over-running outing flags an imminent hard commitment in the nudge."""
    # Outing started 8 min ago, 15-min window -> soft fires now.
    dep = (utcnow() - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
    store.start_outing("getting coffee", 15.0, departure_at=dep)
    # A hard commitment starting in 10 min with a 10-min lead -> at risk.
    store.upsert_commitment(
        title="Team sync", start_at=_utc(10), lead_minutes=10, hardness="hard",
        external_id="work:1",
    )
    item = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
    assert item["fire"] is True
    assert item["level"] == "soft"
    assert item["hard_conflict"] is True
    assert any(i["title"] == "Team sync" for i in item["impact"])
    assert "Team sync" in item["message"]


def test_check_no_impact_when_schedule_clear(client, store):
    """With no nearby commitments, the nudge has no impact tail."""
    dep = (utcnow() - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
    store.start_outing("getting coffee", 15.0, departure_at=dep)
    store.upsert_commitment(title="Tomorrow", start_at=_utc(1440), external_id="work:2")
    item = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
    assert item["impact"] == []
    assert item["hard_conflict"] is False
    assert "at risk" not in item["message"]
