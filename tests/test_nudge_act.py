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


def test_act_pushes_confirmation_feedback(client, store, monkeypatch):
    """A one-tap action pushes its outcome back, so the background tap has feedback.

    An ntfy ``http`` button fires a background GET — the HTML reply is never seen —
    so ``/nudge/act`` also publishes the confirmation to the tapper's own route.
    """
    import prefrontal.delivery as delivery

    sent: list[dict] = []

    def _capture(store_arg, decision, *, handle, **kwargs):
        sent.append({"handle": handle, "text": decision.text})
        return {"handle": handle, "delivered": True, "transport": "ntfy", "detail": ""}

    monkeypatch.setattr(delivery, "deliver_to_member", _capture)

    sid = store.start_focus_session("the API refactor", planned_minutes=25.0)
    token = sign_action(DEFAULT_HANDLE, "focus_end", sid, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")

    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["handle"] == DEFAULT_HANDLE
    assert "Wrapped up" in sent[0]["text"]


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


def test_briefing_returns_signed_feedback_links_but_clean_text(client, store):
    """GET /briefing returns the signed 👍/👎 links but keeps `text` footer-free.

    The footer used to be baked into `text`, where the dashboard's link-less
    markdown renderer printed the raw `[👍 yes](url)` syntax. Now the prose stays
    clean: a rich client renders buttons (POST /briefing/feedback) and text
    channels use these signed links.
    """
    body = client.get("/briefing", headers=_auth()).json()
    assert body["feedback"]["helped_url"].startswith(f"{BASE}/nudge/act?t=")
    assert body["feedback"]["not_helped_url"].startswith(f"{BASE}/nudge/act?t=")
    assert "Did this help?" not in body["text"]
    assert "nudge/act" not in body["text"]  # no raw link syntax leaks into the prose


def test_briefing_feedback_endpoint_records_vote(client, store):
    """POST /briefing/feedback records a vote (the dashboard button path)."""
    from prefrontal.briefing import BRIEFING_HELPFUL_KEY, BRIEFING_NOT_HELPFUL_KEY

    up = client.post("/briefing/feedback", headers=_auth(), json={"helpful": True})
    assert up.status_code == 200 and up.json()["helpful"] == 1
    down = client.post("/briefing/feedback", headers=_auth(), json={"helpful": False})
    assert down.json()["not_helpful"] == 1
    assert store.get_float(BRIEFING_HELPFUL_KEY, 0.0) == 1.0
    assert store.get_float(BRIEFING_NOT_HELPFUL_KEY, 0.0) == 1.0
    # A missing / non-bool `helpful` is a 422, never a silently-miscounted 👎.
    assert client.post("/briefing/feedback", headers=_auth(), json={}).status_code == 422
    assert (
        client.post("/briefing/feedback", headers=_auth(), json={"helpful": "yes"}).status_code
        == 422
    )


def test_act_briefing_feedback_records_and_steers_prompt(client, store):
    """Tapping 👎 (target 0) bumps the tally and tightens the learned guidance."""
    from prefrontal.briefing import (
        BRIEFING_NOT_HELPFUL_KEY,
        learned_briefing_guidance,
    )

    for _ in range(2):
        token = sign_action(DEFAULT_HANDLE, "briefing_not_helped", 0, SIGNING)
        resp = client.get(f"/nudge/act?t={token}")
        assert resp.status_code == 200 and "tighter" in resp.text.lower()

    assert store.get_float(BRIEFING_NOT_HELPFUL_KEY, 0.0) == 2.0
    assert "Tighten up" in learned_briefing_guidance(store)


def test_act_briefing_helped_records_positive(client, store):
    """Tapping 👍 records a helpful vote and thanks the user."""
    from prefrontal.briefing import BRIEFING_HELPFUL_KEY

    token = sign_action(DEFAULT_HANDLE, "briefing_helped", 0, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200 and "helped" in resp.text.lower()
    assert store.get_float(BRIEFING_HELPFUL_KEY, 0.0) == 1.0


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


def test_away_confirm_button_verifies(client, store):
    """The 'away' one-tap button carries a signed away_confirm action on the trip."""
    from prefrontal.webhooks.notify import nudge_actions

    buttons = nudge_actions("away", 7, base_url=BASE, secret=SIGNING, handle=DEFAULT_HANDLE)
    assert [b["label"] for b in buttons] == ["✅ Mark me away"]
    token = buttons[0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == (DEFAULT_HANDLE, "away_confirm", 7)


def test_act_away_confirm_marks_member_away(client, store):
    # An open trip 3 days old → tapping "Mark me away" writes the member window,
    # starting at the departure date and capped, so chores reassign to the partner.
    trip_id = store.open_trip(
        departed_at=(utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    )
    token = sign_action(DEFAULT_HANDLE, "away_confirm", trip_id, SIGNING)
    resp = client.get(f"/nudge/act?t={token}")
    assert resp.status_code == 200
    window = store.member_away_window()
    assert window is not None and window["note"] == "auto-detected trip"


def test_act_away_confirm_is_noop_when_back_home(client, store):
    # No open trip (they're home) → a stale tap marks nobody away.
    resp = client.get(f"/nudge/act?t={sign_action(DEFAULT_HANDLE, 'away_confirm', 999, SIGNING)}")
    assert resp.status_code == 200
    assert store.member_away_window() is None


def test_act_rejects_bad_token(client):
    assert client.get("/nudge/act?t=garbage").status_code == 400
    assert client.get("/nudge/act").status_code == 400


def test_apply_nudge_action_service_returns_headlines():
    """Direct coverage of the extracted service. The endpoint (verified above)
    only adds the signature check, user scoping, and the push-back + HTML page;
    apply_nudge_action owns performing the action and the confirmation headline.
    """
    from prefrontal.nudges import apply_nudge_action

    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        user = {"id": store.user_id}
        settings = Settings(timezone="UTC")
        # Spent-button branch is idempotent (no such focus session).
        assert "already wrapped up" in apply_nudge_action(
            store, "focus_end", 999, user=user, settings=settings
        )
        # A departure outcome and its confirmation.
        assert "made it" in apply_nudge_action(
            store, "made_it", 0, user=user, settings=settings
        ).lower()
        # A no-entity household action.
        assert "okay" in apply_nudge_action(
            store, "star_skip", 0, user=user, settings=settings
        ).lower()


def test_apply_nudge_action_unknown_action_is_a_safe_noop():
    """An unrecognized action must not fall through the made_it/missed_it branch
    and log a spurious 'departure' episode (the old unguarded else) — it returns a
    friendly no-op and touches no departure history."""
    from prefrontal.nudges import apply_nudge_action

    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        user = {"id": store.user_id}
        settings = Settings(timezone="UTC")
        headline = apply_nudge_action(
            store, "some_future_action", 5, user=user, settings=settings
        )
        assert "recognize" in headline.lower()
        # The bug: this used to log a 'departure' miss + dismiss a departure.
        assert [e for e in store.all_episodes() if e["episode_type"] == "departure"] == []
