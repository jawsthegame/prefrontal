"""Tests for closed-loop trip tracking.

Covers the pure reflection classifier and category normalizer, the location
state machine (:func:`prefrontal.trips.process_location`), the trips store
methods, the trip-tracking module's label ask, and the HTTP surface
(``/webhooks/home``, ``/webhooks/location`` detection, ``/trips``,
``/webhooks/trip/{label,reflect}``).
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
from prefrontal.modules.registry import get as get_module
from prefrontal.trips import (
    apply_reflection,
    classify_reflection,
    heuristic_reflection_outcome,
    normalize_trip_category,
    parse_outcome_reply,
    process_location,
    record_trip_return,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

# Home coordinate used across the state-machine tests, and points near/far from it.
HOME = (40.0000, -73.0000)
NEAR = (40.0004, -73.0004)   # ~55 m — inside the default 150 m radius
FAR = (40.0500, -73.0500)    # ~7 km — well outside
FARTHER = (40.0800, -73.0800)  # ~11 km

SECRET = "trips-secret"


def _offline_ollama() -> OllamaClient:
    """An OllamaClient whose every call fails — forces the heuristic path."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_replying(text: str) -> OllamaClient:
    """An OllamaClient whose generate() always returns `text`."""
    return OllamaClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"response": text})
        )
    )


def _minutes_ago(minutes: float) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


# -- pure logic --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("errand", "errand"),
        ("Errand", "errand"),
        ("ERRANDS", "errand"),   # trailing plural snaps onto the canonical
        ("social", "social"),
        ("Vet visit", "vet visit"),  # free text is allowed, just normalized
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_trip_category(value, expected):
    assert normalize_trip_category(value) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("took way longer than I thought and got distracted", "miss"),
        ("ran late, total disaster", "miss"),
        ("bit long but fine", "partial"),
        ("kind of mixed", "partial"),
        ("quick and smooth, in and out", "success"),
        ("went well, productive", "success"),
        ("drove around for a while", None),  # nothing decisive
        # Word-boundary matching: a cue embedded in an unrelated word must not
        # fire. Previously "late" in "related", "fast" in "breakfast", "good" in
        # "goodbye", and "a bit" in "a bitter" all matched via bare substring.
        ("we related, nothing crazy", None),          # not 'late' -> miss
        ("grabbed breakfast on the way", None),         # not 'fast' -> success
        ("said goodbye and headed home", None),         # not 'good' -> success
        ("left a bitter taste honestly", None),         # not 'a bit' -> partial
        # …and the genuine whole-word cues still fire.
        ("all good, no problem", "success"),
        ("ran late the whole time", "miss"),
    ],
)
def test_heuristic_reflection_outcome(text, expected):
    assert heuristic_reflection_outcome(text) == expected


def test_parse_outcome_reply_matches_whole_words():
    """The one-word verdict is matched on boundaries — 'miss' not inside 'dismiss'."""
    assert parse_outcome_reply("MISS") == "miss"
    assert parse_outcome_reply("the verdict is success") == "success"
    assert parse_outcome_reply("please dismiss this") is None  # 'miss' in 'dismiss'


def test_classify_reflection_prefers_llm_then_heuristic():
    """The model leads; a junk/empty reply degrades to the keyword heuristic."""
    good = _ollama_replying("MISS")
    assert classify_reflection("anything at all", client=good) == ("miss", "llm")

    # A reply with no recognizable outcome word -> heuristic on the text.
    junk = _ollama_replying("hmm, hard to say")
    assert classify_reflection("quick and smooth", client=junk) == ("success", "heuristic")

    # No client -> straight to heuristic, then honest no-guess.
    assert classify_reflection("total disaster") == ("miss", "heuristic")
    assert classify_reflection("drove around") == (None, "none")


def test_classify_reflection_blank_is_none():
    assert classify_reflection("   ") == (None, "none")


# -- store + state machine ---------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_process_location_dormant_without_home(store):
    """With no home set, detection is a no-op (nothing to cross)."""
    result = process_location(store, *FAR)
    assert result["event"] is None
    assert result["reason"] == "no_home"
    assert store.active_trip() is None


def test_full_closed_loop(store):
    """Leave home -> away -> return home opens then closes a trip, logs an episode."""
    store.set_home(*HOME)

    # A ping while home does nothing.
    assert process_location(store, *NEAR)["event"] is None
    assert store.active_trip() is None

    # Leaving the radius opens a trip.
    depart = process_location(store, *FAR)
    assert depart["event"] == "depart"
    trip_id = depart["trip_id"]
    assert store.active_trip()["id"] == trip_id

    # A farther ping grows max_distance_m but stays "out".
    away = process_location(store, *FARTHER)
    assert away["event"] is None
    assert store.active_trip()["max_distance_m"] >= away["distance_m"]

    # Returning home closes the loop and logs a pending task episode.
    ret = process_location(store, *NEAR)
    assert ret["event"] == "return"
    assert store.active_trip() is None
    trip = store.get_trip(trip_id)
    assert trip["status"] == "completed"
    assert trip["returned_at"] is not None
    assert trip["episode_id"] == ret["episode_id"]

    episode = store.get_episode(ret["episode_id"])
    assert episode["episode_type"] == "task"
    assert episode["context"] == "trip"
    assert episode["outcome"] is None  # pending until the user reflects


def test_no_passive_trip_while_outing_active(store):
    """A declared outing already tracks the trip, so we don't open a duplicate."""
    store.set_home(*HOME)
    store.start_outing("coffee", 15.0)
    result = process_location(store, *FAR)
    assert result["event"] is None
    assert result["reason"] == "outing_active"
    assert store.active_trip() is None


def test_record_trip_return_actual_no_predicted(store):
    """A trip episode carries actual duration but no prediction (never biases)."""
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(42))
    closed = store.close_trip(trip_id)
    episode_id = record_trip_return(store, closed)
    episode = store.get_episode(episode_id)
    assert episode["predicted_value"] is None
    assert 41.0 <= episode["actual_value"] <= 43.0


def test_unlabeled_then_labeled(store):
    """A completed trip is unlabeled until labeled, then drops out of the ask list."""
    store.set_home(*HOME)
    process_location(store, *FAR)
    trip_id = process_location(store, *FARTHER)["trip_id"] or store.active_trip()["id"]
    process_location(store, *NEAR)

    assert [t["id"] for t in store.unlabeled_trips()] == [trip_id]
    store.label_trip(trip_id, label="Target run", category="errand")
    assert store.unlabeled_trips() == []
    trip = store.get_trip(trip_id)
    assert trip["label"] == "Target run"
    assert trip["category"] == "errand"


def test_apply_reflection_resolves_episode(store):
    """An honest reflection classifies to an outcome and resolves the trip episode."""
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(30))
    closed = store.close_trip(trip_id)
    record_trip_return(store, closed)
    trip = store.get_trip(trip_id)

    result = apply_reflection(store, trip, "took way longer than planned, got distracted")
    assert result["outcome"] == "miss"
    assert result["episode_resolved"] is True

    episode = store.get_episode(trip["episode_id"])
    assert episode["outcome"] == "miss"
    assert episode["acknowledged"] == 1
    # The reflection text is persisted on the trip too.
    assert "distracted" in store.get_trip(trip_id)["reflection"]


def test_apply_reflection_explicit_outcome_overrides(store):
    """An explicit outcome skips the classifier."""
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(10))
    record_trip_return(store, store.close_trip(trip_id))
    trip = store.get_trip(trip_id)
    result = apply_reflection(store, trip, "it was okay honestly", outcome="success")
    assert result["outcome"] == "success"
    assert result["outcome_source"] == "explicit"


def test_apply_reflection_sensor_proposes(store):
    """The raw note is handed to the sensor, which proposes pending updates."""
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(10))
    record_trip_return(store, store.close_trip(trip_id))
    trip = store.get_trip(trip_id)

    sensor = _ollama_replying(
        '[{"kind": "episode", "episode_type": "task", "outcome": "miss", '
        '"context": "errands", "rationale": "I always blow off errands"}]'
    )
    result = apply_reflection(
        store, trip, "I always blow off errands on Mondays", sensor_client=sensor
    )
    assert len(result["proposals"]) == 1
    assert store.list_proposals()  # a pending proposal landed for review


# -- module ------------------------------------------------------------------


def test_module_asks_to_label_unlabeled_trip(store):
    """The module emits one ambient label cue per unlabeled completed trip."""
    from prefrontal.coaching import CoachContext

    store.set_home(*HOME)
    process_location(store, *FAR)
    process_location(store, *NEAR)

    module = get_module("trip_tracking")
    ctx = CoachContext(now=datetime.utcnow())
    cues = module.evaluate(store, ctx)
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "trip_tracking"
    assert cue.urgency == "ambient"
    assert cue.context_key == "trip"
    assert "what was it" in cue.text.lower()

    # Once labeled, the ask stops.
    trip_id = cue.ref["trip_id"]
    store.label_trip(trip_id, label="Groceries", category="errand")
    assert module.evaluate(store, ctx) == []


def _household_store(conn):
    """A store scoped to one co-parent in a two-person household (for away proposals)."""
    from prefrontal.memory.store import provision_user

    root = MemoryStore(conn)
    provision_user(root, "dana", display_name="Dana", token="d")
    provision_user(root, "alex", display_name="Alex", token="a")
    hid = root.create_household("Home")
    root.set_user_household("dana", hid)
    root.set_user_household("alex", hid)
    return root.scoped(root.get_user("dana")["id"])


def test_module_proposes_away_after_multi_day_trip():
    """An open trip past the multi-day threshold yields a one-tap away proposal."""
    from prefrontal.coaching import CoachContext

    conn = init_db(":memory:")
    try:
        store = _household_store(conn)
        store.set_home(*HOME)
        store.open_trip(departed_at=_minutes_ago(3 * 1440))  # away 3 days
        module = get_module("trip_tracking")
        ctx = CoachContext(now=datetime.utcnow())

        cues = [c for c in module.evaluate(store, ctx) if c.context_key == "away_proposal"]
        assert len(cues) == 1
        assert cues[0].ref["trip_id"] == store.active_trip()["id"]
        assert "away" in cues[0].text.lower()

        # Once the member is marked away, the proposal stops (nothing to propose).
        store.set_member_away(starts_on="2000-01-01", ends_on="2999-12-31")
        assert [c for c in module.evaluate(store, ctx)
                if c.context_key == "away_proposal"] == []
    finally:
        conn.close()


def test_module_no_away_proposal_for_short_trip_or_solo_user(store):
    """A brief trip doesn't trigger it, and a user with no household never does."""
    from prefrontal.coaching import CoachContext

    module = get_module("trip_tracking")
    ctx = CoachContext(now=datetime.utcnow())

    # Solo user (the default fixture store), even a long trip → no proposal.
    store.set_home(*HOME)
    store.open_trip(departed_at=_minutes_ago(3 * 1440))
    assert [c for c in module.evaluate(store, ctx) if c.context_key == "away_proposal"] == []

    # Household user, but only an hour out → under the multi-day floor.
    conn = init_db(":memory:")
    try:
        hh = _household_store(conn)
        hh.set_home(*HOME)
        hh.open_trip(departed_at=_minutes_ago(60))
        assert [c for c in module.evaluate(hh, ctx)
                if c.context_key == "away_proposal"] == []
    finally:
        conn.close()


def test_module_profile_section_summarizes(store):
    store.set_home(*HOME)
    trip_id = store.open_trip(departed_at=_minutes_ago(25))
    record_trip_return(store, store.close_trip(trip_id))
    store.label_trip(trip_id, label="Gym", category="health")
    store.set_trip_reflection(trip_id, reflection="went well", outcome="success")

    section = get_module("trip_tracking").profile_section(store)
    assert section is not None
    assert "Closed-loop trips" in section
    assert "health" in section


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_location_endpoint_detects_loop(client):
    """POST /webhooks/home then location pings drive the detection end-to-end."""
    assert client.post("/webhooks/home", json={"lat": HOME[0], "lon": HOME[1]},
                       headers=_auth()).status_code == 201

    depart = client.post(
        "/webhooks/location", json={"lat": FAR[0], "lon": FAR[1]}, headers=_auth()
    ).json()
    assert depart["trip"]["event"] == "depart"
    trip_id = depart["trip"]["trip_id"]

    ret = client.post(
        "/webhooks/location", json={"lat": NEAR[0], "lon": NEAR[1]}, headers=_auth()
    ).json()
    assert ret["trip"]["event"] == "return"

    trips = client.get("/trips", headers=_auth()).json()
    assert trips["active"] is None
    assert trip_id in [t["id"] for t in trips["unlabeled"]]
    assert "errand" in trips["categories"]


def test_location_endpoint_dormant_without_home(client):
    """No home set -> a location ping stores position but reports no trip edge."""
    resp = client.post(
        "/webhooks/location", json={"lat": FAR[0], "lon": FAR[1]}, headers=_auth()
    ).json()
    assert resp["stored"] is True
    assert "trip" not in resp


def test_label_and_reflect_endpoints(client, store):
    client.post("/webhooks/home", json={"lat": HOME[0], "lon": HOME[1]}, headers=_auth())
    client.post("/webhooks/location", json={"lat": FAR[0], "lon": FAR[1]}, headers=_auth())
    trip_id = client.post(
        "/webhooks/location", json={"lat": NEAR[0], "lon": NEAR[1]}, headers=_auth()
    ).json()["trip"]["trip_id"]

    labeled = client.post(
        "/webhooks/trip/label",
        json={"trip_id": trip_id, "label": "Costco run", "category": "Errands"},
        headers=_auth(),
    )
    assert labeled.status_code == 200
    assert labeled.json()["category"] == "errand"  # normalized

    reflected = client.post(
        "/webhooks/trip/reflect",
        json={"trip_id": trip_id, "reflection": "way longer than I meant, got distracted"},
        headers=_auth(),
    )
    body = reflected.json()
    assert body["outcome"] == "miss"
    assert body["episode_resolved"] is True


def test_label_missing_trip_404(client):
    resp = client.post(
        "/webhooks/trip/label",
        json={"trip_id": 999, "label": "x"},
        headers=_auth(),
    )
    assert resp.status_code == 404


def test_label_empty_is_422(client):
    resp = client.post(
        "/webhooks/trip/label",
        json={"trip_id": 1, "label": "   "},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_reflect_missing_trip_404(client):
    resp = client.post(
        "/webhooks/trip/reflect",
        json={"trip_id": 999, "reflection": "fine"},
        headers=_auth(),
    )
    assert resp.status_code == 404


# -- combined retrospective (label + domain + reflection in one call) --------


def _make_trip(client) -> int:
    """Drive a home→away→home loop and return the completed trip's id."""
    client.post("/webhooks/home", json={"lat": HOME[0], "lon": HOME[1]}, headers=_auth())
    client.post("/webhooks/location", json={"lat": FAR[0], "lon": FAR[1]}, headers=_auth())
    return client.post(
        "/webhooks/location", json={"lat": NEAR[0], "lon": NEAR[1]}, headers=_auth()
    ).json()["trip"]["trip_id"]


def test_retro_closes_label_domain_and_reflection_at_once(client):
    """One POST labels, files a domain, and reflects — the whole retrospective."""
    trip_id = _make_trip(client)
    resp = client.post(
        "/webhooks/trip/retro",
        json={
            "trip_id": trip_id,
            "label": "Costco run",
            "category": "Errands",
            "domain": "home",
            "reflection": "way longer than I meant, got distracted",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "Costco run"
    assert body["category"] == "errand"  # normalized
    assert body["domain"] == "home"
    assert body["outcome"] == "miss" and body["episode_resolved"] is True
    assert "Costco run" in body["confirmation"]


def test_retro_defaults_to_newest_unlabeled_trip(client, store):
    """With no trip_id, the retro targets the most recent trip awaiting a label."""
    trip_id = _make_trip(client)
    resp = client.post(
        "/webhooks/trip/retro",
        json={"label": "Pharmacy", "domain": "personal"},
        headers=_auth(),
    )
    assert resp.status_code == 200 and resp.json()["trip_id"] == trip_id
    assert store.get_trip(trip_id)["label"] == "Pharmacy"


def test_retro_domain_only_files_without_a_label(client, store):
    """A domain-only retro files the sphere and leaves the trip unlabeled."""
    trip_id = _make_trip(client)
    resp = client.post(
        "/webhooks/trip/retro", json={"trip_id": trip_id, "domain": "kids"}, headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json()["domain"] == "kids" and resp.json()["label"] is None


def test_retro_requires_at_least_one_field(client):
    trip_id = _make_trip(client)
    resp = client.post(
        "/webhooks/trip/retro", json={"trip_id": trip_id}, headers=_auth()
    )
    assert resp.status_code == 422


def test_retro_explicit_missing_trip_404(client):
    resp = client.post(
        "/webhooks/trip/retro", json={"trip_id": 999, "label": "x"}, headers=_auth()
    )
    assert resp.status_code == 404


def test_retro_no_unlabeled_trip_404(client):
    resp = client.post("/webhooks/trip/retro", json={"label": "x"}, headers=_auth())
    assert resp.status_code == 404
