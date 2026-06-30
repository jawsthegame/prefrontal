"""Tests for the Hyperfocus module's focus-session primitive.

Covers the pure interrupt/protect logic, the ``focus_sessions`` store methods,
and the ``/webhooks/focus/{start,check,end}`` + ``/focus`` endpoints (including
that each interrupt level fires exactly once and that aligned blocks are
protected until the hard ceiling).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.hyperfocus import (
    build_focus_message,
    focus_level,
    is_abandoned,
    is_focus_protected,
    level_rank,
    should_protect,
)
from prefrontal.webhooks.app import create_app

SECRET = "focus-secret"


def _utc_minutes_ago(minutes: float) -> str:
    """A SQLite-style UTC timestamp `minutes` in the past (for started_at)."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _utc_minutes_ahead(minutes: float) -> str:
    """A SQLite-style UTC timestamp `minutes` in the future (for commitments)."""
    return (datetime.utcnow() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


# -- pure logic --------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,planned,expected",
    [
        (10, None, "none"),       # early, no plan -> below the soft block
        (95, None, "check"),      # past the 90-min soft-block default
        (35, 30, "check"),        # past the *planned* 30 min
        (20, 30, "none"),         # still inside the planned window
        (200, 30, "break"),       # past the hard ceiling regardless of plan
        (200, None, "break"),     # hard ceiling dominates
    ],
)
def test_focus_level(elapsed, planned, expected):
    assert focus_level(elapsed, planned_minutes=planned) == expected


def test_focus_level_respects_a_long_plan():
    """A declared 4-hour block is not 'check' at the 90-min default."""
    assert focus_level(100, planned_minutes=240) == "none"


def test_level_rank_orders_severity():
    assert level_rank("none") < level_rank("check") < level_rank("break")


@pytest.mark.parametrize(
    "level,aligned,enabled,expected",
    [
        ("none", True, True, True),    # aligned + healthy -> protect
        ("check", True, True, True),   # still protected during an overrun
        ("break", True, True, False),  # the biological break lifts protection
        ("none", False, True, False),  # misaligned -> never protected
        ("none", True, False, False),  # preference disabled
    ],
)
def test_should_protect(level, aligned, enabled, expected):
    assert should_protect(level, aligned=aligned, protect_enabled=enabled) is expected


def test_is_abandoned():
    # Default ceiling 180, ratio 2.0 -> abandoned at 360 min.
    assert not is_abandoned(300, 180, 2.0)
    assert is_abandoned(360, 180, 2.0)
    assert not is_abandoned(100, 0, 2.0)  # no ceiling -> never abandoned


def test_build_focus_message_varies_by_level_and_alignment():
    aligned = build_focus_message("check", task="the refactor", elapsed_minutes=95, aligned=True)
    assert "Still the thing" in aligned and "the refactor" in aligned
    redirect = build_focus_message("check", task="email", elapsed_minutes=95, aligned=False)
    assert "park it" in redirect
    brk = build_focus_message("break", task="x", elapsed_minutes=200, name="Tom")
    assert brk.startswith("Hey Tom") and "Stand up" in brk
    assert build_focus_message("none", task="x", elapsed_minutes=5) == ""


def test_is_focus_protected_reflects_active_sessions():
    """The live helper protects an aligned healthy block and not a misaligned one."""
    with MemoryStore.open(":memory:") as store:
        assert is_focus_protected(store) is False  # nothing active
        store.start_focus_session("deep work", started_at=_utc_minutes_ago(30))
        assert is_focus_protected(store) is True
    with MemoryStore.open(":memory:") as store:
        store.start_focus_session(
            "doomscrolling", aligned=False, started_at=_utc_minutes_ago(30)
        )
        assert is_focus_protected(store) is False


# -- store -------------------------------------------------------------------


def test_focus_session_store_lifecycle():
    """Start -> active (with elapsed) -> close (with actual minutes + breadcrumb)."""
    with MemoryStore.open(":memory:") as store:
        sid = store.start_focus_session(
            "the API refactor", planned_minutes=60, started_at=_utc_minutes_ago(12)
        )
        active = store.active_focus_sessions()
        assert len(active) == 1
        assert active[0]["id"] == sid
        assert active[0]["aligned"] == 1
        assert active[0]["elapsed_minutes"] == pytest.approx(12, abs=0.5)

        closed = store.close_focus_session(
            sid, breadcrumb="finish the migration", outcome="worth_it"
        )
        assert closed["status"] == "ended"
        assert closed["actual_minutes"] == pytest.approx(12, abs=0.5)
        assert closed["breadcrumb"] == "finish the migration"
        assert closed["outcome"] == "worth_it"
        # No longer active; double-close is a no-op.
        assert store.active_focus_sessions() == []
        assert store.close_focus_session(sid) is None


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


def test_start_creates_session(client):
    resp = client.post(
        "/webhooks/focus/start",
        json={"intended_task": "the API refactor", "planned_minutes": 90},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == 1
    assert body["intended_task"] == "the API refactor"
    assert body["planned_minutes"] == 90
    assert body["aligned"] is True


def test_start_rejects_blank_task(client):
    resp = client.post(
        "/webhooks/focus/start", json={"intended_task": "   "}, headers=_auth()
    )
    assert resp.status_code == 422


def test_check_protects_aligned_block(store, client):
    """An aligned block inside its plan fires nothing and reports protect=true."""
    store.start_focus_session(
        "deep work", planned_minutes=120, started_at=_utc_minutes_ago(30)
    )
    resp = client.post("/webhooks/focus/check", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["protect"] is True
    item = body["active"][0]
    assert item["level"] == "none"
    assert item["fire"] is False
    assert item["protect"] is True


def test_check_fires_each_level_once(store, client):
    """A new level fires once; an immediate re-poll does not refire it."""
    # Past the 90-min default soft block, below the 180-min ceiling -> 'check'.
    store.start_focus_session("a tangent", started_at=_utc_minutes_ago(100))
    first = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert first["level"] == "check" and first["fire"] is True
    assert first["message"]
    second = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert second["level"] == "check" and second["fire"] is False


def test_check_break_fires_and_is_not_protected(store, client):
    """Past the hard ceiling a break fires and protection lifts even when aligned."""
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(200))
    body = client.post("/webhooks/focus/check", headers=_auth()).json()
    item = body["active"][0]
    assert item["level"] == "break" and item["fire"] is True
    assert item["protect"] is False
    assert body["protect"] is False


def test_check_abandoned_auto_closes(store, client):
    """A session left open far past the ceiling auto-closes as abandoned."""
    # Default ceiling 180 * ratio 2.0 = 360 min.
    sid = store.start_focus_session("forgotten", started_at=_utc_minutes_ago(400))
    item = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert item["status"] == "abandoned"
    assert store.get_focus_session(sid)["status"] == "abandoned"


def test_end_logs_episode_with_outcome(store, client):
    """Ending a session logs a task episode and persists breadcrumb + rating."""
    sid = store.start_focus_session(
        "the refactor", planned_minutes=60, started_at=_utc_minutes_ago(75)
    )
    resp = client.post(
        "/webhooks/focus/end",
        json={
            "session_id": sid,
            "outcome": "worth_it",
            "breadcrumb": "wire up the poller next",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ended"
    assert body["actual_minutes"] == pytest.approx(75, abs=0.5)
    assert body["breadcrumb"] == "wire up the poller next"
    assert body["outcome"] == "success"  # worth_it -> success
    episode = store.get_episode(body["episode_id"])
    assert episode["episode_type"] == "task"
    assert episode["predicted_value"] == 60
    assert episode["outcome"] == "success"
    assert "next: wire up the poller next" in episode["notes"]


def test_end_defaults_to_most_recent_active(store, client):
    store.start_focus_session("first", started_at=_utc_minutes_ago(20))
    sid2 = store.start_focus_session("second", started_at=_utc_minutes_ago(5))
    body = client.post("/webhooks/focus/end", json={}, headers=_auth()).json()
    assert body["session_id"] == sid2


def test_end_without_active_session_is_404(client):
    resp = client.post("/webhooks/focus/end", json={}, headers=_auth())
    assert resp.status_code == 404


def test_focus_list_reports_active_and_recent(store, client):
    store.start_focus_session("ongoing", planned_minutes=120, started_at=_utc_minutes_ago(30))
    done = store.start_focus_session("done", started_at=_utc_minutes_ago(60))
    store.close_focus_session(done, outcome="should_have_stopped")
    body = client.get("/focus", headers=_auth()).json()
    assert len(body["active"]) == 1
    assert body["active"][0]["intended_task"] == "ongoing"
    assert body["active"][0]["protect"] is True
    assert any(r["intended_task"] == "done" for r in body["recent"])


def test_focus_list_has_no_side_effects(store, client):
    """GET /focus never fires or records a level (unlike /check)."""
    sid = store.start_focus_session("a tangent", started_at=_utc_minutes_ago(100))
    client.get("/focus", headers=_auth())
    assert store.get_focus_session(sid)["last_level"] == "none"


def test_focus_endpoints_require_auth(client):
    assert client.post("/webhooks/focus/start", json={"intended_task": "x"}).status_code == 401
    assert client.post("/webhooks/focus/check").status_code == 401
    assert client.post("/webhooks/focus/end", json={}).status_code == 401
    assert client.get("/focus").status_code == 401


# -- cross-module: protected hyperfocus suppresses non-critical outing nudges --


def _outing_item(client, status_code=200):
    """POST the outing check and return the single active outing item."""
    resp = client.post("/webhooks/outing/check", json={}, headers=_auth())
    assert resp.status_code == status_code
    return resp.json()["active"][0]


def test_protected_focus_defers_soft_outing_nudge(store, client):
    """A soft outing nudge is held back (not cancelled) while focus is protected."""
    # window 20, out 12 min -> ratio 0.6 -> soft level, would normally fire.
    oid = store.start_outing("coffee", 20.0, departure_at=_utc_minutes_ago(12))
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(30))

    item = _outing_item(client)
    assert item["level"] == "soft"
    assert item["fire"] is False
    assert item["suppressed_by_focus"] is True
    # Deferred, not cancelled: the level is not recorded, so it can fire later.
    assert store.get_outing(oid)["last_level"] == "none"


def test_unprotected_soft_outing_nudge_still_fires(store, client):
    """Without an aligned focus block, the same soft nudge fires normally."""
    store.start_outing("coffee", 20.0, departure_at=_utc_minutes_ago(12))
    item = _outing_item(client)
    assert item["level"] == "soft"
    assert item["fire"] is True
    assert item["suppressed_by_focus"] is False


def test_firm_outing_nudge_punches_through_focus(store, client):
    """A firm escalation is critical — it fires even while focus is protected."""
    # window 10, out 12 min -> ratio 1.2 -> firm.
    store.start_outing("errand", 10.0, departure_at=_utc_minutes_ago(12))
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(30))
    item = _outing_item(client)
    assert item["level"] == "firm"
    assert item["fire"] is True
    assert item["suppressed_by_focus"] is False


def test_hard_commitment_punches_through_focus(store, client):
    """A soft nudge with a hard commitment at risk is not suppressed."""
    store.start_outing("coffee", 20.0, departure_at=_utc_minutes_ago(12))
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(30))
    # Hard commitment so soon that the realistic return blows its lead time.
    store.upsert_commitment(
        title="dentist",
        start_at=_utc_minutes_ahead(10),
        lead_minutes=10,
        hardness="hard",
        source="manual",
    )
    item = _outing_item(client)
    assert item["level"] == "soft"
    assert item["hard_conflict"] is True
    assert item["fire"] is True
    assert item["suppressed_by_focus"] is False
