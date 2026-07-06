"""Tests for one-tap nudge *actions* from an ntfy notification button.

Covers the signed action token (:func:`sign_action`/:func:`verify_action`), the
``actions`` fields surfaced by the outing / focus / departure check endpoints,
and the ``GET /nudge/act`` endpoint that runs the tapped action (wrap up, I'm
back, made it …) with the same close/record logic as its full POST endpoint.
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
from prefrontal.webhooks.oauth import _sign, sign_action, verify_action
from tests.conftest import DEFAULT_HANDLE, scoped_default

SECRET = "act-webhook-secret"
SIGNING = "act-signing-key"
BASE = "https://mac-mini.tailnet.ts.net"


# -- signed token round-trip -------------------------------------------------


def test_sign_verify_action_round_trip():
    token = sign_action("tom", "focus_end", 42, SIGNING)
    assert verify_action(token, SIGNING) == ("tom", "focus_end", 42)


def test_verify_action_rejects_wrong_secret_and_garbage():
    token = sign_action("tom", "outing_return", 42, SIGNING)
    assert verify_action(token, "other-key") is None
    assert verify_action("not-a-token", SIGNING) is None
    assert verify_action("", SIGNING) is None


def test_verify_action_rejects_expired():
    token = sign_action("tom", "made_it", 7, SIGNING, now=0.0)
    assert verify_action(token, SIGNING, now=13 * 3600) is None


def test_verify_action_rejects_action_outside_allowlist():
    # Hand-sign an action the endpoint must never dispatch on.
    token = _sign("tom|delete_everything|1", SIGNING, 3600)
    assert verify_action(token, SIGNING) is None


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


# -- responses carry signed action buttons -----------------------------------


def test_outing_check_includes_signed_actions(client, store):
    store.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
    item = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
    actions = item["actions"]
    assert [a["label"] for a in actions] == ["I'm back", "Abandon"]
    token = actions[0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == (DEFAULT_HANDLE, "outing_return", item["outing_id"])


def test_departure_check_includes_signed_actions(client, store):
    store.upsert_commitment(
        title="Standup", start_at=_utc(5), lead_minutes=10.0, source="manual"
    )
    reminder = client.post("/webhooks/departure/check", json={}, headers=_auth()).json()["reminder"]
    assert [a["label"] for a in reminder["actions"]] == ["Made it", "Missed it"]


def test_focus_check_includes_wrap_up_action(client, store):
    store.start_focus_session("the API refactor", planned_minutes=25.0)
    item = client.post("/webhooks/focus/check", json={}, headers=_auth()).json()["active"][0]
    assert [a["label"] for a in item["actions"]] == ["Wrap up"]


def test_actions_empty_when_not_configured():
    """Without a public origin + signing key, no buttons are minted."""
    conn = init_db(":memory:")
    try:
        scoped = scoped_default(MemoryStore(conn))
        scoped.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
        app = create_app(store=scoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            item = c.post("/webhooks/outing/check", json={}, headers=_auth()).json()["active"][0]
            assert item["actions"] == []
    finally:
        conn.close()


# -- GET /nudge/act dispatch --------------------------------------------------


def test_act_focus_end_wraps_up_session(client, store):
    sid = store.start_focus_session("the API refactor", planned_minutes=25.0)
    token = sign_action(DEFAULT_HANDLE, "focus_end", sid, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200 and "Wrapped up" in resp.text
    assert store.get_focus_session(sid)["status"] == "ended"
    # Re-tapping the spent button is idempotent (friendly page, no 500).
    again = client.get(f"/nudge/act?t={token}")
    assert again.status_code == 200 and "already wrapped up" in again.text


def test_act_outing_return_closes_outing(client, store):
    oid = store.start_outing("getting coffee", 15.0, home_lat=0.0, home_lon=0.0)
    token = sign_action(DEFAULT_HANDLE, "outing_return", oid, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200 and "Welcome back" in resp.text
    assert store.get_outing(oid)["status"] == "returned"


def test_act_outing_abandon_closes_outing(client, store):
    oid = store.start_outing("errand", 15.0, home_lat=0.0, home_lon=0.0)
    token = sign_action(DEFAULT_HANDLE, "outing_abandon", oid, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    assert store.get_outing(oid)["status"] == "abandoned"


def test_act_made_it_and_missed_it_log_departure_outcome(client, store):
    store.upsert_commitment(
        title="Dentist", start_at=_utc(30), lead_minutes=10.0, source="manual"
    )
    cid = store.upcoming_commitments()[0]["id"]

    made = client.get(f"/nudge/act?t={sign_action(DEFAULT_HANDLE, 'made_it', cid, SIGNING)}")
    assert made.status_code == 200 and "made it" in made.text.lower()
    eps = store.episodes_by_type("departure")
    assert eps and eps[0]["outcome"] == "success"
    # And it dismisses further departure nudges for that commitment.
    assert cid in store.dismissed_departures()


def test_focus_switch_pause_includes_resolve_actions(client, store):
    sid = store.start_focus_session("the API refactor", planned_minutes=25.0)
    pause = client.post(
        "/webhooks/focus/switch", json={"session_id": sid}, headers=_auth()
    ).json()
    assert [a["label"] for a in pause["actions"]] == [
        "Stay on task", "Park it", "Switch anyway"
    ]
    token = pause["actions"][0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == (DEFAULT_HANDLE, "switch_return", sid)


def test_act_switch_return_keeps_the_block(client, store):
    sid = store.start_focus_session("deep work", planned_minutes=25.0)
    resp = client.get(f"/nudge/act?t={sign_action(DEFAULT_HANDLE, 'switch_return', sid, SIGNING)}")
    assert resp.status_code == 200 and "Staying on" in resp.text
    assert store.get_focus_session(sid)["status"] == "active"  # unchanged


def test_act_switch_defer_parks_a_todo_and_stays(client, store):
    sid = store.start_focus_session("deep work", planned_minutes=25.0)
    resp = client.get(f"/nudge/act?t={sign_action(DEFAULT_HANDLE, 'switch_defer', sid, SIGNING)}")
    assert resp.status_code == 200 and "Parked it" in resp.text
    assert store.get_focus_session(sid)["status"] == "active"  # still on the block
    parked = [t for t in store.open_todos() if t.get("source") == "impulse"]
    assert len(parked) == 1


def test_act_switch_switch_closes_the_block(client, store):
    sid = store.start_focus_session("deep work", planned_minutes=25.0)
    resp = client.get(f"/nudge/act?t={sign_action(DEFAULT_HANDLE, 'switch_switch', sid, SIGNING)}")
    assert resp.status_code == 200 and "Switched away" in resp.text
    assert store.get_focus_session(sid)["status"] == "switched"


def test_act_rejects_bad_token(client):
    assert client.get("/nudge/act?t=garbage").status_code == 400
    assert client.get("/nudge/act").status_code == 400
