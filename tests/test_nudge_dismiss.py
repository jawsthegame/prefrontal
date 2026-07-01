"""Tests for one-tap nudge dismissal from a Pushover notification.

Covers the signed dismiss token (:func:`sign_dismiss`/:func:`verify_dismiss`),
the ``dismissed_departures`` store helpers, the ``dismiss_url`` fields surfaced
by ``/webhooks/outing/check`` and ``/webhooks/departure/check``, and the
``GET /nudge/dismiss`` endpoint that acts on a tapped link.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.oauth import sign_dismiss, verify_dismiss
from tests.conftest import DEFAULT_HANDLE, scoped_default

SECRET = "nudge-webhook-secret"
SIGNING = "nudge-signing-key"
BASE = "https://agent-1.tail8b0a.ts.net"


# -- signed token round-trip -------------------------------------------------


def test_sign_verify_round_trip():
    token = sign_dismiss("tom", "outing", 42, SIGNING)
    assert verify_dismiss(token, SIGNING) == ("tom", "outing", 42)


def test_verify_rejects_wrong_secret():
    token = sign_dismiss("tom", "outing", 42, SIGNING)
    assert verify_dismiss(token, "other-key") is None


def test_verify_rejects_expired_token():
    token = sign_dismiss("tom", "departure", 7, SIGNING, now=0.0)
    # Far past the 12h TTL.
    assert verify_dismiss(token, SIGNING, now=13 * 3600) is None


def test_verify_rejects_unknown_kind():
    # Hand-sign a value with a kind the endpoint must never dispatch on.
    from prefrontal.webhooks.oauth import _sign

    token = _sign("tom|todo|1", SIGNING, 3600)
    assert verify_dismiss(token, SIGNING) is None


def test_verify_rejects_garbage():
    assert verify_dismiss("not-a-token", SIGNING) is None
    assert verify_dismiss("", SIGNING) is None


# -- store helpers -----------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_dismissed_departures_is_a_set_and_idempotent(store):
    assert store.dismissed_departures() == set()
    store.dismiss_departure(5)
    store.dismiss_departure(5)  # idempotent
    store.dismiss_departure(9)
    assert store.dismissed_departures() == {5, 9}


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(
        store=store,
        settings=Settings(
            webhook_secret=SECRET, session_secret=SIGNING, oauth_base_url=BASE
        ),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _utc(delta_minutes: float) -> str:
    return (utcnow() + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_outing_check_includes_signed_dismiss_url(client, store):
    store.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
    item = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
    url = item["dismiss_url"]
    assert url.startswith(f"{BASE}/nudge/dismiss?t=")
    token = url.split("t=", 1)[1]
    assert verify_dismiss(token, SIGNING) == (DEFAULT_HANDLE, "outing", item["outing_id"])


def test_departure_check_includes_signed_dismiss_url(client, store):
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), lead_minutes=10.0, source="manual"
    )
    reminder = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()["reminder"]
    url = reminder["dismiss_url"]
    token = url.split("t=", 1)[1]
    assert verify_dismiss(token, SIGNING) == (
        DEFAULT_HANDLE,
        "departure",
        reminder["commitment_id"],
    )


def test_dismiss_url_empty_when_not_configured():
    """Without a public origin + signing key, no link is minted (feature off)."""
    conn = init_db(":memory:")
    try:
        scoped = scoped_default(MemoryStore(conn))
        scoped.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
        app = create_app(store=scoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            item = c.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
            assert item["dismiss_url"] == ""
    finally:
        conn.close()


def test_dismiss_outing_pins_escalation(client, store):
    outing_id = store.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
    token = sign_dismiss(DEFAULT_HANDLE, "outing", outing_id, SIGNING)

    resp = client.get(f"/nudge/dismiss?t={token}")
    assert resp.status_code == 200
    assert "getting coffee" in resp.text
    # Escalation is pinned at the ceiling so no further nudge can out-rank it,
    # but the outing stays active (not closed).
    outing = store.get_outing(outing_id)
    assert outing["last_level"] == "call"
    assert outing["status"] == "active"


def test_dismiss_departure_suppresses_future_nudge(client, store):
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Dentist",
        start_at=_utc(8),
        dest_lat=0.0,
        dest_lon=0.0,
        location="123 Main St",
        source="manual",
    )
    first = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert first["fire"] is True
    cid = first["reminder"]["commitment_id"]

    token = sign_dismiss(DEFAULT_HANDLE, "departure", cid, SIGNING)
    assert client.get(f"/nudge/dismiss?t={token}").status_code == 200

    # The dismissed commitment is no longer even a candidate, so nothing fires.
    after = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()
    assert after["fire"] is False
    assert after["reminder"] is None


def test_dismiss_rejects_bad_token(client):
    assert client.get("/nudge/dismiss?t=garbage").status_code == 400
    assert client.get("/nudge/dismiss").status_code == 400


def test_dismiss_unknown_outing_is_404(client):
    token = sign_dismiss(DEFAULT_HANDLE, "outing", 999, SIGNING)
    assert client.get(f"/nudge/dismiss?t={token}").status_code == 404
