"""Tests for the Location-Aware Task Anchor module.

Covers the pure escalation logic and time-window parser, the outings store
methods, and the /webhooks/outing/{start,check,return} endpoints (including that
each escalation level fires exactly once).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.location_anchor import (
    DEFAULT_INFERRED_WINDOW_MINUTES,
    build_message,
    escalation_level,
    haversine_m,
    heuristic_time_window,
    infer_time_window,
    is_abandoned,
    is_at_home,
    parse_time_window,
)
from prefrontal.webhooks.app import create_app


def _offline_ollama() -> OllamaClient:
    """An OllamaClient whose every call fails — forces the heuristic/default path."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_replying(text: str) -> OllamaClient:
    """An OllamaClient whose generate() returns `text` (for the LLM path)."""
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))

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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("grabbing a coffee", 15.0),
        ("walk the dog", 20.0),
        ("quick grocery run", 45.0),
        ("heading to the gym", 60.0),
        ("just stepping out", None),  # no keyword
    ],
)
def test_heuristic_time_window(text, expected):
    """The keyword heuristic maps common errands to typical durations."""
    assert heuristic_time_window(text) == expected


def test_infer_prefers_llm_then_falls_back():
    """LLM answer wins; on a bad/empty reply it degrades to heuristic/default."""
    good = _ollama_replying("20")
    assert infer_time_window("anything", client=good) == (20.0, "llm")

    # Out-of-bounds reply is discarded -> heuristic ('coffee').
    bad = _ollama_replying("99999")
    assert infer_time_window("grab a coffee", client=bad) == (15.0, "heuristic")

    # No client at all -> straight to heuristic, then default.
    assert infer_time_window("grab a coffee") == (15.0, "heuristic")
    assert infer_time_window("visiting a friend") == (DEFAULT_INFERRED_WINDOW_MINUTES, "default")


def test_infer_blank_intention_is_none():
    """A blank intention yields None (nothing to reason from)."""
    assert infer_time_window("   ") is None
    assert infer_time_window("") is None


def test_infer_no_fallback_returns_none_on_model_miss():
    """With fallback disabled, an unusable model reply gives None (no default)."""
    bad = _ollama_replying("no idea")
    assert infer_time_window("grab a coffee", client=bad, fallback=False) is None


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


@pytest.mark.parametrize(
    ("distance", "radius", "expected"),
    [(0, 150, True), (149, 150, True), (151, 150, False), (None, 150, False)],
)
def test_is_at_home(distance, radius, expected):
    """Within the radius (and only with a known distance) counts as home."""
    assert is_at_home(distance, radius) is expected


@pytest.mark.parametrize(
    ("elapsed", "window", "ratio", "expected"),
    [(44, 15, 3.0, False), (45, 15, 3.0, True), (60, 15, 3.0, True), (10, 0, 3.0, False)],
)
def test_is_abandoned(elapsed, window, ratio, expected):
    """Abandoned once elapsed reaches window * ratio (never for a zero window)."""
    assert is_abandoned(elapsed, window, ratio) is expected


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
    """A TestClient wired to the injected store with auth enabled.

    The Ollama client is offline so tests never touch the network; windowless
    starts therefore degrade deterministically to the heuristic/default.
    """
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )
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


def test_start_infers_window_when_unparseable(client):
    """A non-parseable intention is no longer rejected — the window is inferred.

    The fixture's Ollama is offline, so this exercises the heuristic/default
    fallback: 'just heading out' matches no keyword and lands on the default.
    """
    resp = client.post(
        "/webhooks/outing/start",
        json={"intention": "just heading out"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["time_window_minutes"] == DEFAULT_INFERRED_WINDOW_MINUTES
    assert body["time_window_source"] == "default"


def test_start_infers_window_from_heuristic(client):
    """A keyword intention ('coffee') gets the heuristic window when offline."""
    resp = client.post(
        "/webhooks/outing/start",
        json={"intention": "grabbing a coffee"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["time_window_minutes"] == 15.0
    assert body["time_window_source"] == "heuristic"


def test_start_infers_window_via_llm(store):
    """When the model answers, its estimate wins over the heuristic/default."""
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_ollama_replying("About 25 minutes"),
    )
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/outing/start",
            json={"intention": "returning a package"},
            headers=_auth(),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["time_window_minutes"] == 25.0
    assert body["time_window_source"] == "llm"


def test_start_rejects_blank_intention(client):
    """A blank intention has nothing to infer from, so it's still a 422."""
    resp = client.post(
        "/webhooks/outing/start",
        json={"intention": "   "},
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


def test_check_at_home_passively_closes(client, store):
    """Being within the home radius closes the outing (returned) without nudging."""
    store.start_outing(
        "getting coffee", 15.0, home_lat=0.0, home_lon=0.0,
        departure_at=_utc_minutes_ago(8),
    )
    resp = client.post(
        "/webhooks/outing/check",
        json={"current_lat": 0.0001, "current_lon": 0.0},  # ~11 m from home
        headers=_auth(),
    ).json()
    item = resp["active"][0]
    assert item["at_home"] is True
    assert item["fire"] is False
    assert item["status"] == "returned"
    assert item["outcome"] == "success"  # 8 min within the 15 min window
    # No longer active, and a return episode was logged.
    assert store.active_outings() == []
    assert store.recent_episodes(1)[0]["episode_type"] == "task"


def test_check_abandoned_auto_closes(client, store):
    """Far past the abandon ratio closes the outing as abandoned, no nudge."""
    # 20 min into a 5 min window = 400% > 300% abandon ratio.
    store.start_outing("getting coffee", 5.0, departure_at=_utc_minutes_ago(20))
    resp = client.post("/webhooks/outing/check", json={}, headers=_auth()).json()
    item = resp["active"][0]
    assert item["status"] == "abandoned"
    assert item["fire"] is False
    assert item["outcome"] == "miss"
    assert store.active_outings() == []
    # Abandoned logs a miss with no actual duration (doesn't pollute estimates).
    ep = store.recent_episodes(1)[0]
    assert ep["outcome"] == "miss"
    assert ep["actual_value"] is None


def test_check_away_still_escalates(client, store):
    """A location away from home does not suppress time-based escalation."""
    store.start_outing(
        "getting coffee", 10.0, home_lat=0.0, home_lon=0.0,
        departure_at=_utc_minutes_ago(16),  # 160% -> call
    )
    resp = client.post(
        "/webhooks/outing/check",
        json={"current_lat": 0.05, "current_lon": 0.0},  # ~5.5 km away
        headers=_auth(),
    ).json()
    item = resp["active"][0]
    assert item["at_home"] is False
    assert item["status"] == "active"
    assert item["level"] == "call"
    assert item["fire"] is True


def test_return_without_active_outing_is_404(client):
    """Returning with nothing active is a 404."""
    assert client.post("/webhooks/outing/return", json={}, headers=_auth()).status_code == 404


def test_outing_endpoints_require_auth(client):
    """All anchor endpoints are token-guarded."""
    assert client.post("/webhooks/outing/start", json={"intention": "x"}).status_code == 401
    assert client.post("/webhooks/outing/check", json={}).status_code == 401
    assert client.post("/webhooks/outing/return", json={}).status_code == 401
