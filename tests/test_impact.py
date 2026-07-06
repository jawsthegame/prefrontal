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
    cascade_at_risk,
    cascade_impact,
    cascade_phrase,
    fragile_stretch,
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


# -- cascade / domino propagation --------------------------------------------

_T = datetime(2026, 7, 4, 9, 0, 0)


def _at(minutes: float) -> str:
    """Stored-format UTC timestamp `minutes` after the fixed base `_T`."""
    return (_T + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _cc(title, start_min, lead=10.0, dur_min=None, hardness="soft", cid=1):
    c = {
        "id": cid,
        "title": title,
        "start_at": _at(start_min),
        "lead_minutes": lead,
        "hardness": hardness,
    }
    if dur_min is not None:
        c["end_at"] = _at(start_min + dur_min)
    return c


def test_cascade_propagates_through_the_chain():
    """A late finish pushes the next commitment even when it wasn't a collision."""
    # Free at _T. A starts +5 (10-min lead) so you're 5 min late; it runs 30 min,
    # finishing at +40. B starts at +45 — reachable from _T on its own, but the
    # pushed-back finish of A leaves you late for it too.
    a = _cc("A", 5, dur_min=30, cid=1)
    b = _cc("B", 45, cid=2)

    chain = cascade_impact(_T, [a, b])
    assert [c.commitment["title"] for c in chain] == ["A", "B"]  # schedule order
    ca, cb = chain
    assert ca.at_risk and ca.caused_by is None  # first domino: root is the overrun
    assert cb.at_risk and cb.caused_by == "A"   # knocked on by A's overrun
    assert cb.delay_minutes > 0


def test_cascade_self_heals_across_a_gap():
    """Enough slack before a commitment ends the chain — its delay resets to 0."""
    a = _cc("A", 5, dur_min=30, cid=1)          # runs late, ends ~+40
    later = _cc("Later", 300, cid=2)            # hours away, absorbs the slip
    chain = cascade_impact(_T, [a, later])
    tail = chain[1]
    assert tail.commitment["title"] == "Later"
    assert not tail.at_risk
    assert tail.delay_minutes == 0
    assert tail.caused_by is None


def test_cascade_phrase_shows_multi_link_chain():
    """Several at-risk links render as an arrow chain naming each."""
    a = _cc("A", 5, dur_min=30, cid=1)
    b = _cc("B", 45, cid=2)
    phrase = cascade_phrase(cascade_impact(_T, [a, b]))
    assert "cascades" in phrase and "→" in phrase
    assert "'A'" in phrase and "'B'" in phrase


def test_cascade_phrase_single_and_empty():
    """One link reads like a plain heads-up; a clear chain is empty."""
    a = _cc("Team sync", 5, dur_min=30, cid=1)
    single = cascade_phrase(cascade_impact(_T, [a]))
    assert "Team sync" in single and "at risk" in single
    clear = _cc("Tomorrow", 1440, cid=1)
    assert cascade_phrase(cascade_impact(_T, [clear])) == ""
    assert cascade_at_risk(cascade_impact(_T, [clear])) == []


def test_cascade_lead_override_replaces_static_lead():
    """A travel-based lead override can flag a leg the static lead called safe."""
    # Starts in 20 min with a 5-min static lead → reachable from now.
    c = _cc("Client site", 20, lead=5, dur_min=30, cid=7)
    assert not cascade_impact(_T, [c])[0].at_risk
    # But if the real drive is 30 min, you can't make it (20 − 30 = −10).
    risky = cascade_impact(_T, [c], lead_override={7: 30})
    assert risky[0].at_risk and round(risky[0].slack_minutes) == -10


# -- fragile stretch (briefing preview) --------------------------------------


def test_fragile_stretch_flags_bias_inflated_collision():
    """Back-to-backs that fit on paper topple once each runs bias× long."""
    # Two 30-min meetings, 35 min apart — 5 min of buffer. On paper they hold.
    a = _cc("Design review", 0, dur_min=30, cid=1)
    b = _cc("1:1", 35, dur_min=30, cid=2)
    # No overrun (1.0) → nothing flagged; the day holds as scheduled.
    assert fragile_stretch([a, b], 1.0) == []
    # At 1.4×, Design review runs ~42 min and eats the buffer, pushing the 1:1.
    risky = fragile_stretch([a, b], 1.4)
    assert [i.commitment["title"] for i in risky] == ["1:1"]
    assert risky[0].caused_by == "Design review"
    assert risky[0].delay_minutes > 0


def test_fragile_stretch_silent_when_spaced_out():
    """A day with real gaps stays silent even under a heavy bias."""
    a = _cc("Morning", 0, dur_min=30, cid=1)
    b = _cc("Afternoon", 300, dur_min=30, cid=2)  # hours later
    assert fragile_stretch([a, b], 1.6) == []


def test_fragile_stretch_needs_two_commitments():
    """Nothing to collide with a single commitment."""
    assert fragile_stretch([_cc("Solo", 0, dur_min=30)], 1.5) == []


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


def test_cascade_endpoint_propagates_and_reports_source(client, store):
    """GET /impact/cascade projects a chain from an explicit free-time."""
    # A starts in 5 min (10-min lead) and runs 30 min; B starts in 45 min. With
    # over_minutes=0 (free now) you're late to A, which pushes you late for B.
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), end_at=_utc(35), lead_minutes=10,
        hardness="hard", external_id="work:c1",
    )
    store.upsert_commitment(
        title="Review", start_at=_utc(45), lead_minutes=10, external_id="work:c2",
    )
    body = client.get("/impact/cascade?over_minutes=0", headers=_auth()).json()
    assert body["source"] == "over_minutes"
    titles = [c["title"] for c in body["cascade"]]
    assert titles == ["Standup", "Review"]          # schedule order
    at_risk_titles = [c["title"] for c in body["at_risk"]]
    assert at_risk_titles == ["Standup", "Review"]  # both toppled
    review = next(c for c in body["cascade"] if c["title"] == "Review")
    assert review["caused_by"] == "Standup"         # named upstream domino
    assert body["hard_conflict"] is True            # Standup is hard
    assert "cascades" in body["phrase"]


def test_cascade_endpoint_travel_aware_with_location(client, store):
    """A known location + destination coords make the cascade use real travel legs."""
    store.upsert_commitment(
        title="Gym", start_at=_utc(30), dest_lat=1.0, dest_lon=1.0, external_id="work:g",
    )
    aware = client.get(
        "/impact/cascade?over_minutes=0&current_lat=0&current_lon=0", headers=_auth()
    ).json()
    assert aware["travel_aware"] is True
    # With no location (none passed, none stored), it falls back to static leads.
    fallback = client.get("/impact/cascade?over_minutes=0", headers=_auth()).json()
    assert fallback["travel_aware"] is False


def test_cascade_endpoint_falls_back_to_now_when_schedule_clear(client, store):
    """With no free-time hint and nothing tight, the chain is empty and calm."""
    store.upsert_commitment(title="Tomorrow", start_at=_utc(1440), external_id="work:c3")
    body = client.get("/impact/cascade", headers=_auth()).json()
    assert body["source"] == "now"
    assert body["at_risk"] == []
    assert body["hard_conflict"] is False
    assert body["phrase"] == ""


def test_cascade_endpoint_excludes_fyi_commitments(client, store):
    """An FYI event (where someone else will be) never topples your schedule."""
    # If counted, this FYI's assumed length would run right into the real
    # commitment after it and flag it late — but an FYI is never yours to attend,
    # so it must not consume your time (the same rule every other surface makes).
    store.upsert_commitment(
        title="Kids at grandma's", start_at=_utc(5), kind="fyi", external_id="cal:fyi",
    )
    store.upsert_commitment(
        title="Tax", start_at=_utc(20), lead_minutes=10, external_id="work:tax",
    )
    body = client.get("/impact/cascade?over_minutes=0", headers=_auth()).json()
    titles = [c["title"] for c in body["cascade"]]
    assert "Kids at grandma's" not in titles  # FYI excluded from the walk
    assert body["at_risk"] == []              # Tax alone has slack, nothing topples


def test_cascade_endpoint_excludes_placeholder_holds(client, store):
    """A generic HOLD/OOO block is elastic time, never a topple in the chain."""
    # Standup runs long (55 min) and would push anything sitting after it. A
    # bare "HOLD" block in its shadow must NOT be walked as a real commitment
    # that topples — it's elastic time you'd yield the moment a real meeting
    # needed it (is_attendable excludes it), so it neither appears in the chain
    # nor manufactures an at-risk domino.
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), end_at=_utc(60), lead_minutes=0,
        external_id="work:standup",
    )
    store.upsert_commitment(title="HOLD", start_at=_utc(20), external_id="cal:hold")
    body = client.get("/impact/cascade?over_minutes=0", headers=_auth()).json()
    titles = [c["title"] for c in body["cascade"]]
    assert "HOLD" not in titles                              # excluded from the walk
    assert all(c["title"] != "HOLD" for c in body["at_risk"])


def test_cascade_endpoint_does_not_reach_across_days(client, store):
    """A tight back-to-back tomorrow isn't something you're 'behind' on today."""
    # Two back-to-back commitments a full day out. Seeded at now, an unbounded
    # walk would flag the second as toppled — but being behind doesn't carry
    # across a night's sleep, so the seed day (today) is empty and calm.
    store.upsert_commitment(title="Cindy", start_at=_utc(1440), external_id="cal:cindy")
    store.upsert_commitment(
        title="Tax", start_at=_utc(1470), lead_minutes=10, external_id="work:tax2",
    )
    body = client.get("/impact/cascade", headers=_auth()).json()
    assert body["cascade"] == []
    assert body["at_risk"] == []


def test_cascade_endpoint_rejects_bad_free_at(client, store):
    """A malformed free_at is a 422, never a silent now-fallback."""
    r = client.get("/impact/cascade?free_at=not-a-time", headers=_auth())
    assert r.status_code == 422


def test_check_no_impact_when_schedule_clear(client, store):
    """With no nearby commitments, the nudge has no impact tail."""
    dep = (utcnow() - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
    store.start_outing("getting coffee", 15.0, departure_at=dep)
    store.upsert_commitment(title="Tomorrow", start_at=_utc(1440), external_id="work:2")
    item = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
    assert item["impact"] == []
    assert item["hard_conflict"] is False
    assert "at risk" not in item["message"]
