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
    LocationAnchorModule,
    apply_outing_evaluation,
    build_message,
    escalation_level,
    evaluate_outing,
    haversine_m,
    heuristic_time_window,
    infer_time_window,
    is_abandoned,
    is_at_home,
    parse_time_window,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default


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
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
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


def test_close_outing_backdates_to_home_arrival():
    """A returned close backdates to home_arrived_at when set (not 'now')."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        oid = store.start_outing(
            "getting coffee", 15.0, departure_at=_utc_minutes_ago(20)
        )
        arrived = _utc_minutes_ago(14)  # left 20 min ago, home 6 min later
        store.set_outing_home_arrived(oid, arrived)
        closed = store.close_outing(oid, status="returned")
        assert closed["returned_at"] == arrived
        assert closed["actual_minutes"] == pytest.approx(6, abs=0.5)


def test_close_outing_explicit_returned_at_wins():
    """An explicit returned_at overrides both 'now' and the arrival stamp."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        oid = store.start_outing(
            "getting coffee", 15.0, departure_at=_utc_minutes_ago(30)
        )
        store.set_outing_home_arrived(oid, _utc_minutes_ago(6))
        explicit = _utc_minutes_ago(10)
        closed = store.close_outing(oid, status="returned", returned_at=explicit)
        assert closed["returned_at"] == explicit


def test_abandon_close_is_not_backdated():
    """Only a returned close backdates; an abandon still stamps 'now'."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        oid = store.start_outing(
            "getting coffee", 5.0, departure_at=_utc_minutes_ago(30)
        )
        store.set_outing_home_arrived(oid, _utc_minutes_ago(6))
        closed = store.close_outing(oid, status="abandoned")
        # actual_minutes ~ 30 (now - departure), not 24 (arrival - departure).
        assert closed["actual_minutes"] == pytest.approx(30, abs=1.0)


def test_set_outing_home_arrived_clears_and_only_touches_active():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        oid = store.start_outing("getting coffee", 15.0)
        store.set_outing_home_arrived(oid, _utc_minutes_ago(2))
        assert store.get_outing(oid)["home_arrived_at"] is not None
        store.set_outing_home_arrived(oid, None)  # cleared (left home again)
        assert store.get_outing(oid)["home_arrived_at"] is None
        # A closed outing is not re-stamped.
        store.close_outing(oid, status="abandoned")
        store.set_outing_home_arrived(oid, _utc_minutes_ago(1))
        assert store.get_outing(oid)["home_arrived_at"] is None


def test_profile_section_excludes_abandoned_outings():
    """Errand punctuality counts only genuinely-returned outings.

    Regression: an abandoned outing is auto-closed with a `returned_at` stamp
    too, so the old `if o.get("returned_at")` filter counted it — fabricating a
    duration and inflating the over-window rate, the very thing
    `record_outing_abandoned` deliberately avoids.
    """
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        # One real return that ran over its 5-min window (departed 20 min ago).
        returned = store.start_outing(
            "getting coffee", 5.0, departure_at=_utc_minutes_ago(20)
        )
        store.close_outing(returned, status="returned")
        # One abandonment of an identical window — never actually returned.
        abandoned = store.start_outing(
            "getting milk", 5.0, departure_at=_utc_minutes_ago(20)
        )
        store.close_outing(abandoned, status="abandoned")

        section = LocationAnchorModule().profile_section(store)
        # Only the returned outing is counted, so the denominator is 1, not 2.
        assert "1/1 recent outings ran over" in section
        assert "2/2" not in section


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store():
    """An in-memory store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
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


def test_start_confirmation_is_exact_for_stated_window(client):
    """An explicit window yields a confirmation with no 'estimated' hedge."""
    body = client.post(
        "/webhooks/outing/start",
        json={"intention": "lunch", "time_window_minutes": 45},
        headers=_auth(),
    ).json()
    conf = body["confirmation"]
    assert "lunch" in conf and "45 min" in conf
    assert "estimated" not in conf  # the window was stated, not guessed
    assert "~" not in conf


def test_start_confirmation_flags_a_guessed_window(client):
    """An inferred window is read back as estimated so the user can correct it."""
    body = client.post(
        "/webhooks/outing/start",
        json={"intention": "grabbing a coffee"},  # heuristic -> 15 min
        headers=_auth(),
    ).json()
    assert body["time_window_source"] == "heuristic"
    conf = body["confirmation"]
    assert "estimated" in conf and "~15 min" in conf


def test_start_files_explicit_domain(client, store):
    """An explicit domain is stored on the outing and read back in the confirmation."""
    body = client.post(
        "/webhooks/outing/start",
        json={"intention": "lunch", "time_window_minutes": 45, "domain": "home"},
        headers=_auth(),
    ).json()
    assert body["domain"] == "home"
    assert "Filed under home." in body["confirmation"]
    assert store.active_outings()[0]["domain"] == "home"


def test_start_infers_domain_from_intention(client, store):
    """A clear sphere in the intention pre-files the outing without an explicit domain."""
    body = client.post(
        "/webhooks/outing/start",
        json={"intention": "swim with the kids", "time_window_minutes": 60},
        headers=_auth(),
    ).json()
    assert body["domain"] == "kids"
    assert "Filed under kids." in body["confirmation"]
    assert store.active_outings()[0]["domain"] == "kids"


def test_start_leaves_domain_unassigned_when_ambiguous(client, store):
    """A domain-less intention isn't force-fit — it stays unassigned for later."""
    body = client.post(
        "/webhooks/outing/start",
        json={"intention": "grabbing a coffee", "time_window_minutes": 15},
        headers=_auth(),
    ).json()
    assert body["domain"] is None
    assert "Filed under" not in body["confirmation"]
    assert store.active_outings()[0]["domain"] is None


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

    # The fired nudge was recorded once and surfaces via GET /nudges.
    nudges = client.get("/nudges", headers=_auth()).json()["nudges"]
    assert len(nudges) == 1
    assert nudges[0]["kind"] == "outing"
    assert nudges[0]["level"] == "soft"
    assert nudges[0]["message"] == first["active"][0]["message"]


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
    # The read-back names the overrun so a Shortcut can show it verbatim.
    assert "Welcome back" in body["confirmation"]
    assert "18 min" in body["confirmation"]


def test_check_at_home_prompts_then_grace_closes(client, store):
    """Arriving home asks first (stays active); a later poll past grace closes it,
    backdated to the first arrival rather than the acknowledgement."""
    oid = store.start_outing(
        "getting coffee", 15.0, home_lat=0.0, home_lon=0.0,
        departure_at=_utc_minutes_ago(8),
    )
    at_home = {"current_lat": 0.0001, "current_lon": 0.0}  # ~11 m from home
    # First poll home: prompt fires, outing stays active, arrival stamped.
    first = client.post("/webhooks/outing/check", json=at_home, headers=_auth()).json()
    item = first["active"][0]
    assert item["at_home"] is True
    assert item["fire"] is True and "wrap up" in item["message"]
    assert item["status"] == "active"
    arrived = store.get_outing(oid)["home_arrived_at"]
    assert arrived is not None
    assert store.active_outings()  # not closed on arrival

    # Grace elapsed with no answer → auto-close as returned, timed to arrival.
    store.set_state("home_arrive_grace_minutes", "0")
    second = client.post("/webhooks/outing/check", json=at_home, headers=_auth()).json()
    item = second["active"][0]
    assert item["status"] == "returned"
    assert item["outcome"] == "success"  # ~8 min out, within the 15 min window
    assert store.active_outings() == []
    closed = store.get_outing(oid)
    assert closed["returned_at"] == arrived  # backdated, not "now"
    assert store.recent_episodes(1)[0]["episode_type"] == "task"


def test_check_home_return_backdates_actual_minutes(client, store):
    """The logged actual time out is measured to arrival, not the ack tap."""
    oid = store.start_outing(
        "getting coffee", 15.0, home_lat=0.0, home_lon=0.0,
        departure_at=_utc_minutes_ago(8),
    )
    store.set_state("home_arrive_grace_minutes", "0")
    at_home = {"current_lat": 0.0001, "current_lon": 0.0}
    client.post("/webhooks/outing/check", json=at_home, headers=_auth())  # stamp + prompt
    client.post("/webhooks/outing/check", json=at_home, headers=_auth())  # grace close
    # Episode's actual ≈ minutes to arrival (~8), well within the 15-min window.
    ep = store.recent_episodes(1)[0]
    assert ep["actual_value"] == pytest.approx(8, abs=1.5)
    assert ep["outcome"] == "success"
    assert store.get_outing(oid)["status"] == "returned"


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
    assert client.get("/outings").status_code == 401


# -- monitoring dashboard ----------------------------------------------------


def test_outings_endpoint_reports_active_and_recent(client, store):
    """GET /outings returns active outings (with computed level) + recent ones."""
    store.start_outing("getting coffee", 10.0, departure_at=_utc_minutes_ago(6))  # 60% -> soft
    oid = store.start_outing("dog walk", 20.0)
    store.close_outing(oid, status="returned")

    data = client.get("/outings", headers=_auth()).json()
    assert [o["intention"] for o in data["active"]] == ["getting coffee"]
    assert data["active"][0]["level"] == "soft"
    # The closed one shows up in recent history.
    recent_closed = [o for o in data["recent"] if o["status"] == "returned"]
    assert any(o["intention"] == "dog walk" for o in recent_closed)


def test_outings_endpoint_has_no_side_effects(client, store):
    """Polling /outings never fires nudges or closes outings (unlike /check).

    A 20-min-old 5-min outing is well past the abandon ratio: /check would close
    it as abandoned, but /outings must leave it untouched.
    """
    store.start_outing("getting coffee", 5.0, departure_at=_utc_minutes_ago(20))
    client.get("/outings", headers=_auth())
    client.get("/outings", headers=_auth())
    # Still active, still no episodes logged.
    assert len(store.active_outings()) == 1
    assert store.recent_episodes(1) == []


def test_dashboard_page_served_without_auth(client):
    """The dashboard shell is plain HTML, needs no token, and carries no data."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Prefrontal" in resp.text
    assert "X-Prefrontal-Token" in resp.text  # it asks for the token client-side


def test_kids_lens_served_without_auth(client):
    """The read-only Kids lens is plain HTML, needs no token, and carries no data."""
    resp = client.get("/kids")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'const LENS = "kids"' in resp.text          # bound to the kids focus
    assert "/household/sheet" in resp.text              # reads the shared sheet
    assert "X-Prefrontal-Token" in resp.text            # asks for the token client-side


def test_pets_lens_served_and_bound_to_pets(client):
    """The Pets lens is its own read-only projection, bound to the pets focus."""
    resp = client.get("/pets")
    assert resp.status_code == 200
    assert 'const LENS = "pets"' in resp.text
    # Its own projection: pet-flavoured category labels + an age helper, not a
    # relabeled Kids page.
    assert "PET_CATEGORY_LABELS" in resp.text and "Vet & meds" in resp.text
    assert "function petAge" in resp.text


def test_family_redirects_to_household(client):
    """The retired /family route redirects to the writable Household hub."""
    resp = client.get("/family", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/household"


# -- shared per-outing decision (evaluate_outing / apply_outing_evaluation) ---


def _outing(**kw):
    """A minimal active-outing dict for the pure decision function."""
    base = {
        "id": 1, "intention": "coffee", "elapsed_minutes": 0.0,
        "time_window_minutes": 20.0, "last_level": "none",
        "home_lat": 0.0, "home_lon": 0.0, "departure_at": "2026-07-02 12:00:00",
        "home_arrived_at": None,
    }
    base.update(kw)
    return base


def test_evaluate_outing_prompts_on_first_arrival_home():
    """First time home: don't close — stamp arrival and fire the 'end it?' prompt."""
    ev = evaluate_outing(
        _outing(elapsed_minutes=5.0), cur_lat=0.0, cur_lon=0.0, home_radius=100.0,
        abandon_ratio=3.0, bias=1.0, commitments=[],
    )
    assert ev.action == "at_home" and ev.at_home is True
    assert ev.fire is True and "wrap up" in ev.message
    assert ev.arrived_at is not None  # timestamp to record


def test_evaluate_outing_auto_returns_after_grace_backdated():
    """Home past the grace window with no answer → return, backdated to arrival."""
    from prefrontal.impact import utcnow

    arrived = (utcnow() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    ev = evaluate_outing(
        _outing(elapsed_minutes=25.0, home_arrived_at=arrived),
        cur_lat=0.0, cur_lon=0.0, home_radius=100.0, abandon_ratio=3.0, bias=1.0,
        commitments=[], home_grace_minutes=10.0,
    )
    assert ev.action == "return"
    assert ev.returned_at == arrived  # close time is when we first saw them home


def test_evaluate_outing_reprompts_within_grace():
    """Home but still inside the grace window: re-ask, don't close yet."""
    from prefrontal.impact import utcnow

    arrived = (utcnow() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    ev = evaluate_outing(
        _outing(elapsed_minutes=13.0, home_arrived_at=arrived),
        cur_lat=0.0, cur_lon=0.0, home_radius=100.0, abandon_ratio=3.0, bias=1.0,
        commitments=[], home_grace_minutes=10.0,
    )
    assert ev.action == "at_home" and ev.fire is True
    assert ev.arrived_at is None  # already stamped; don't re-stamp


def test_evaluate_outing_resets_stale_arrival_when_left_home():
    """Left home again before ending it → clear the stale arrival stamp."""
    ev = evaluate_outing(
        _outing(elapsed_minutes=8.0, home_arrived_at="2026-07-02 12:03:00"),
        cur_lat=0.05, cur_lon=0.0, home_radius=100.0,  # ~5.5 km away
        abandon_ratio=3.0, bias=1.0, commitments=[],
    )
    assert ev.action == "active" and ev.at_home is False
    assert ev.reset_home_arrived is True


def test_evaluate_outing_abandons_far_past_window():
    ev = evaluate_outing(
        _outing(elapsed_minutes=70.0), cur_lat=None, cur_lon=None, home_radius=100.0,
        abandon_ratio=3.0, bias=1.0, commitments=[],  # 70 > 3×20
    )
    assert ev.action == "abandon"


def test_evaluate_outing_fires_a_new_level():
    # 22 min into a 20-min window → past 100% ("firm"), a new level over "none".
    ev = evaluate_outing(
        _outing(elapsed_minutes=22.0, last_level="none"), cur_lat=None, cur_lon=None,
        home_radius=100.0, abandon_ratio=3.0, bias=1.0, commitments=[],
    )
    assert ev.action == "active" and ev.level == "firm"
    assert ev.fire is True and ev.message  # a nudge is due, with text
    # Already at that level → no re-fire.
    ev2 = evaluate_outing(
        _outing(elapsed_minutes=22.0, last_level="firm"), cur_lat=None, cur_lon=None,
        home_radius=100.0, abandon_ratio=3.0, bias=1.0, commitments=[],
    )
    assert ev2.fire is False


def test_evaluate_outing_travel_lead_override_flags_reachable_looking_commitment():
    """A travel-lead override can flag a commitment the static lead called safe."""
    from prefrontal.impact import utcnow

    now = utcnow()
    fmt = "%Y-%m-%d %H:%M:%S"
    # departure_at in the past → projected free-time is now; over its window so it fires.
    outing = _outing(
        elapsed_minutes=22.0, time_window_minutes=20.0,
        departure_at=(now - timedelta(minutes=60)).strftime(fmt),
    )
    # Starts in 20 min with a 5-min static lead → reachable; a 30-min drive isn't.
    c = {
        "id": 9, "title": "Client site", "hardness": "soft", "lead_minutes": 5.0,
        "start_at": (now + timedelta(minutes=20)).strftime(fmt),
    }
    kw = dict(cur_lat=None, cur_lon=None, home_radius=100.0, abandon_ratio=3.0, bias=1.0)
    static = evaluate_outing(outing, commitments=[c], **kw)
    assert static.impacts == []  # safe on the flat 5-min lead
    travel = evaluate_outing(outing, commitments=[c], lead_override={9: 30.0}, **kw)
    assert any(i["title"] == "Client site" for i in travel.impacts)  # flagged by real drive


def test_apply_outing_evaluation_advances_level_and_records(client, store):
    oid = store.start_outing("coffee", 20.0, home_lat=0.0, home_lon=0.0)
    outing = next(o for o in store.active_outings() if o["id"] == oid)
    ev = evaluate_outing(
        {**outing, "elapsed_minutes": 22.0, "last_level": "none"},
        cur_lat=None, cur_lon=None, home_radius=100.0, abandon_ratio=3.0,
        bias=1.0, commitments=[],
    )
    status_, outcome = apply_outing_evaluation(store, outing, ev)
    assert status_ == "active" and outcome is None
    assert store.get_outing(oid)["last_level"] == "firm"  # one-fire advanced
