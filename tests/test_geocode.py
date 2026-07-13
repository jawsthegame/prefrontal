"""Tests for commitment geocoding — places → cache → network, and enrichment.

Covers the pure normalization/place-matching/resolution logic, the store's
places + geocode-cache helpers, the Nominatim client (via a mock transport), and
the HTTP surface (curated places, the opt-in network fallback, and the
backfill/enrichment wiring on calendar sync + manual add).
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.geocode import (
    GeocodeResult,
    enrich_commitments,
    match_place,
    normalize_query,
    resolve_location,
)
from prefrontal.impact import utcnow
from prefrontal.integrations.nominatim import GeocoderError, NominatimGeocoder
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "geocode-secret"


class StubGeocoder:
    """A geocoder that records calls and returns a fixed result."""

    def __init__(self, result: tuple[float, float] | None = (40.0, -73.0)):
        self.result = result
        self.calls: list[str] = []

    def geocode(self, query: str):
        self.calls.append(query)
        return self.result


class RaisingGeocoder:
    """A geocoder whose calls always fail (transport error)."""

    def __init__(self):
        self.calls: list[str] = []

    def geocode(self, query: str):
        self.calls.append(query)
        raise GeocoderError("boom")


# -- pure functions ----------------------------------------------------------


def test_normalize_query():
    assert normalize_query("123 Main St.") == "123 main st"
    assert normalize_query("  GYM!!  ") == "gym"
    assert normalize_query(None) == ""


def test_match_place_matches_location_and_title():
    places = [{"name": "gym", "lat": 1.0, "lon": 2.0}]
    assert match_place(places, "Downtown Gym", None) == (1.0, 2.0)
    assert match_place(places, None, "Gym session") == (1.0, 2.0)
    assert match_place(places, "Dentist", "Checkup") is None


def test_match_place_prefers_most_specific():
    """Longest-name-first ordering means the specific alias wins."""
    places = [  # as MemoryStore.places() returns: longest name first
        {"name": "dentist office", "lat": 9.0, "lon": 9.0},
        {"name": "office", "lat": 1.0, "lon": 1.0},
    ]
    assert match_place(places, "Dentist Office", None) == (9.0, 9.0)


def test_match_place_is_whole_phrase_not_substring():
    """'office' should not match 'officer' (word-boundary phrase match)."""
    places = [{"name": "office", "lat": 1.0, "lon": 1.0}]
    assert match_place(places, "meet the officer", None) is None


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_resolve_location_prefers_curated_place(store):
    store.add_place("gym", 1.0, 2.0)
    geo = StubGeocoder()
    result = resolve_location(store, "My Gym", geocoder=geo)
    assert result == GeocodeResult((1.0, 2.0), "place")
    assert geo.calls == []  # never hit the network


def test_resolve_location_uses_cache(store):
    store.set_geocode_cache(normalize_query("123 Main St"), 5.0, 6.0)
    geo = StubGeocoder()
    result = resolve_location(store, "123 Main St", geocoder=geo)
    assert result == GeocodeResult((5.0, 6.0), "cache")
    assert geo.calls == []


def test_resolve_location_respects_cached_miss(store):
    store.set_geocode_cache(normalize_query("nowhere"), None, None)
    geo = StubGeocoder()
    result = resolve_location(store, "Nowhere", geocoder=geo)
    assert result.coords is None and result.basis == "cache_miss"
    assert geo.calls == []  # a recorded miss is not retried


def test_resolve_location_geocodes_and_caches(store):
    geo = StubGeocoder(result=(7.0, 8.0))
    result = resolve_location(store, "456 Elm Ave", geocoder=geo)
    assert result == GeocodeResult((7.0, 8.0), "geocoded")
    assert geo.calls == ["456 Elm Ave"]
    # Second call is served from cache, not the network.
    again = resolve_location(store, "456 Elm Ave", geocoder=geo)
    assert again.basis == "cache"
    assert geo.calls == ["456 Elm Ave"]


def test_resolve_location_caches_not_found(store):
    geo = StubGeocoder(result=None)
    result = resolve_location(store, "Atlantis", geocoder=geo)
    assert result.coords is None and result.basis == "not_found"
    # The miss is cached so a later sync doesn't re-ask.
    again = resolve_location(store, "Atlantis", geocoder=geo)
    assert again.basis == "cache_miss"
    assert geo.calls == ["Atlantis"]


def test_resolve_location_error_does_not_cache(store):
    geo = RaisingGeocoder()
    result = resolve_location(store, "Flaky Place", geocoder=geo)
    assert result.coords is None and result.basis == "error"
    # Nothing cached -> a later attempt retries.
    assert store.get_geocode_cache(normalize_query("Flaky Place")) is None


def test_resolve_location_disabled_without_geocoder(store):
    result = resolve_location(store, "Somewhere", geocoder=None)
    assert result.coords is None and result.basis == "disabled"


def test_resolve_location_empty(store):
    assert resolve_location(store, None).basis == "empty"
    assert resolve_location(store, "   ").basis == "empty"


def _utc(delta_minutes: float) -> str:
    return (utcnow() + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_enrich_commitments_fills_coords(store):
    store.add_place("gym", 1.0, 2.0)
    store.upsert_commitment(title="Gym session", start_at=_utc(60), location="gym", source="manual")
    store.upsert_commitment(title="Mystery", start_at=_utc(90), location="???", source="manual")
    out = enrich_commitments(store, geocoder=StubGeocoder(result=None))
    assert out["considered"] == 2
    assert out["resolved"] == 1  # only the curated-place one resolved
    rows = store.upcoming_commitments()
    gym = next(c for c in rows if c["title"] == "Gym session")
    assert (gym["dest_lat"], gym["dest_lon"]) == (1.0, 2.0)


def test_enrich_commitments_skips_those_with_coords(store):
    store.upsert_commitment(
        title="Has coords", start_at=_utc(60), location="x", dest_lat=1.0, dest_lon=2.0,
        source="manual",
    )
    out = enrich_commitments(store, geocoder=StubGeocoder())
    assert out["considered"] == 0


# -- Nominatim client (mock transport) ---------------------------------------


def test_nominatim_parses_top_result():
    def handler(request):
        return httpx.Response(200, json=[{"lat": "40.5", "lon": "-73.2"}])

    geo = NominatimGeocoder(transport=httpx.MockTransport(handler))
    assert geo.geocode("somewhere") == (40.5, -73.2)


def test_nominatim_empty_results_is_none():
    geo = NominatimGeocoder(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    assert geo.geocode("nowhere") is None


def test_nominatim_http_error_raises():
    geo = NominatimGeocoder(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(GeocoderError):
        geo.geocode("x")


def test_nominatim_malformed_result_raises():
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json=[{"name": "no coords"}]))
    geo = NominatimGeocoder(transport=bad)
    with pytest.raises(GeocoderError):
        geo.geocode("x")


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def stub():
    return StubGeocoder(result=(40.0, -73.0))


@pytest.fixture()
def client(store, stub):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET), geocoder=stub)
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_places_crud_and_auth(client):
    assert client.post("/places", json={"name": "gym", "lat": 1, "lon": 2}).status_code == 401
    resp = client.post("/places", json={"name": "The Gym", "lat": 1.0, "lon": 2.0}, headers=_auth())
    assert resp.status_code == 201 and resp.json()["name"] == "the gym"
    places = client.get("/places", headers=_auth()).json()["places"]
    assert places[0]["label"] == "The Gym"


def test_place_add_list_delete_and_404(client):
    """The web dashboard's full lifecycle: add → list → delete → 404 on missing."""
    # Add two places (one with a friendly label).
    client.post("/places", json={"name": "The Gym", "lat": 1.0, "lon": 2.0}, headers=_auth())
    client.post("/places", json={"name": "dentist", "lat": 3.0, "lon": 4.0}, headers=_auth())
    names = {p["name"] for p in client.get("/places", headers=_auth()).json()["places"]}
    assert names == {"the gym", "dentist"}

    # Delete by the original spelling — the path name is normalized, matching the
    # stored key, so "The Gym" removes "the gym". The space is percent-encoded, as
    # a real HTTP client would send it.
    resp = client.delete("/places/The%20Gym", headers=_auth())
    assert resp.status_code == 200 and resp.json() == {"deleted": "the gym"}
    names = {p["name"] for p in client.get("/places", headers=_auth()).json()["places"]}
    assert names == {"dentist"}

    # Deleting an absent place is a 404 (nothing was removed).
    assert client.delete("/places/the%20gym", headers=_auth()).status_code == 404
    # And the route is auth-gated like the rest.
    assert client.delete("/places/dentist").status_code == 401


def test_place_relabel_and_coords_via_upsert(client):
    """Relabeling / fixing coordinates is a plain re-POST (upsert by name)."""
    client.post("/places", json={"name": "gym", "lat": 1.0, "lon": 2.0}, headers=_auth())
    client.post(
        "/places",
        json={"name": "gym", "lat": 9.0, "lon": 8.0, "label": "The Fancy Gym"},
        headers=_auth(),
    )
    places = client.get("/places", headers=_auth()).json()["places"]
    assert len(places) == 1
    assert places[0]["label"] == "The Fancy Gym"
    assert (places[0]["lat"], places[0]["lon"]) == (9.0, 8.0)


def test_delete_place_is_scoped_per_user(unscoped):
    """One user's delete can't remove another user's place (store-level scope)."""
    from prefrontal.memory.store import provision_user

    ua, _ = provision_user(unscoped, "alice", display_name="Alice")
    ub, _ = provision_user(unscoped, "bob", display_name="Bob")
    a = unscoped.scoped(ua["id"])
    b = unscoped.scoped(ub["id"])

    a.add_place("gym", 1.0, 2.0)
    b.add_place("gym", 3.0, 4.0)

    # Bob deleting "gym" removes only his own; Alice's survives untouched.
    assert b.delete_place("gym") is True
    assert [p["name"] for p in b.places()] == []
    assert [(p["name"], p["lat"]) for p in a.places()] == [("gym", 1.0)]

    # Deleting again (now absent for Bob) reports nothing removed.
    assert b.delete_place("gym") is False


@pytest.fixture()
def unscoped():
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


def test_manual_commitment_resolves_via_place_offline(client, store, stub):
    """A curated place resolves a manual commitment without geocoding enabled."""
    client.post("/places", json={"name": "gym", "lat": 1.0, "lon": 2.0}, headers=_auth())
    resp = client.post(
        "/commitments",
        json={"title": "Gym", "start_at": _utc(60), "location": "the gym"},
        headers=_auth(),
    ).json()
    assert (resp["dest_lat"], resp["dest_lon"]) == (1.0, 2.0)
    assert stub.calls == []  # offline path; network never touched


def test_calendar_sync_geocodes_when_enabled(client, store, stub):
    """With geocoding enabled, sync resolves free-text locations and caches them."""
    store.set_state("geocoding_enabled", "1")
    body = {
        "events": [
            {
                "title": "Dentist",
                "start_at": _utc(120),
                "external_id": "c:1",
                "location": "123 Main St",
            }
        ]
    }
    resp = client.post("/webhooks/calendar/sync", json=body, headers=_auth()).json()
    assert resp["geocoded"] == 1
    assert stub.calls == ["123 Main St"]
    commitments = client.get("/commitments", headers=_auth()).json()["commitments"]
    assert (commitments[0]["dest_lat"], commitments[0]["dest_lon"]) == (40.0, -73.0)
    # Re-sync hits the cache, not the network.
    client.post("/webhooks/calendar/sync", json=body, headers=_auth())
    assert stub.calls == ["123 Main St"]


def test_calendar_sync_no_network_when_disabled(client, store, stub):
    """Geocoding off (default): the network geocoder is never called."""
    body = {
        "events": [
            {
                "title": "Dentist",
                "start_at": _utc(120),
                "external_id": "c:2",
                "location": "123 Main St",
            }
        ]
    }
    resp = client.post("/webhooks/calendar/sync", json=body, headers=_auth()).json()
    assert resp["geocoded"] == 0
    assert stub.calls == []


def test_commitments_geocode_backfills(client, store, stub):
    """The explicit backfill endpoint resolves pending commitments."""
    store.set_state("geocoding_enabled", "1")
    store.upsert_commitment(title="Dentist", start_at=_utc(60), location="123 Main St")
    out = client.post("/commitments/geocode", headers=_auth()).json()
    assert out == {"considered": 1, "resolved": 1}
