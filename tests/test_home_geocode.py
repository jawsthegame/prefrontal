"""HTTP surface for setting the home location + the address picker.

Covers reading/writing the home coordinate from the web UI (`GET`/`POST /home`),
the geocoding opt-in (`POST /home/geocoding`), and the address search that backs
the picker (`GET /geocode/search`) — including that it stays off the network
until the user opts in.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

_SECRET = "home-http-secret"


def _auth() -> dict[str, str]:
    return {"X-Prefrontal-Token": _SECRET}


class _StubGeocoder:
    """A geocoder that records calls and returns canned candidates."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 5) -> list[dict[str, object]]:
        self.calls.append((query, limit))
        return [
            {"display_name": f"{query}, Springfield, USA", "lat": 40.1, "lon": -73.2},
            {"display_name": f"{query}, Shelbyville, USA", "lat": 41.0, "lon": -74.0},
        ]


def _app():
    store = scoped_default(MemoryStore(init_db(":memory:")))
    geo = _StubGeocoder()
    app = create_app(store=store, settings=Settings(webhook_secret=_SECRET), geocoder=geo)
    return TestClient(app), store, geo


def test_home_starts_unset_and_geocoding_off():
    client, _, _ = _app()
    with client:
        data = client.get("/home", headers=_auth()).json()
        assert data == {"home": None, "geocoding_enabled": False}


def test_post_home_sets_the_coordinate():
    client, store, _ = _app()
    with client:
        data = client.post("/home", json={"lat": 37.42, "lon": -122.08}, headers=_auth()).json()
        assert data["home"] == {"lat": 37.42, "lon": -122.08}
        # Persisted where trip detection reads it.
        assert store.get_home() == {"lat": 37.42, "lon": -122.08}


def test_search_is_inert_until_opted_in():
    """With geocoding off, search returns nothing and never calls the geocoder."""
    client, _, geo = _app()
    with client:
        data = client.get("/geocode/search?q=main+st", headers=_auth()).json()
        assert data == {"enabled": False, "results": []}
        assert geo.calls == []  # the address never left the box


def test_search_returns_candidates_once_enabled():
    client, _, geo = _app()
    with client:
        toggled = client.post("/home/geocoding", json={"enabled": True}, headers=_auth()).json()
        assert toggled["geocoding_enabled"] is True
        data = client.get("/geocode/search?q=1600+amphitheatre", headers=_auth()).json()
        assert data["enabled"] is True
        assert len(data["results"]) == 2
        assert data["results"][0]["display_name"].startswith("1600 amphitheatre")
        assert geo.calls and geo.calls[0][0] == "1600 amphitheatre"


def test_search_empty_query_returns_no_results_without_a_lookup():
    client, _, geo = _app()
    with client:
        client.post("/home/geocoding", json={"enabled": True}, headers=_auth())
        data = client.get("/geocode/search?q=%20%20", headers=_auth()).json()
        assert data == {"enabled": True, "results": []}
        assert geo.calls == []  # blank query short-circuits before the network


def test_geocoding_toggle_off_again():
    client, store, _ = _app()
    with client:
        client.post("/home/geocoding", json={"enabled": True}, headers=_auth())
        # Stored as the codebase-standard "1"/"0" (seed default + tests use these),
        # not "true"/"false" — consistent with docs and manual DB edits.
        assert store.get_state("geocoding_enabled") == "1"
        data = client.post("/home/geocoding", json={"enabled": False}, headers=_auth()).json()
        assert data["geocoding_enabled"] is False
        assert store.get_state("geocoding_enabled") == "0"
        assert store.get_bool("geocoding_enabled", True) is False


def test_home_endpoints_require_auth():
    client, _, _ = _app()
    with client:
        assert client.get("/home").status_code == 401
        assert client.post("/home", json={"lat": 1.0, "lon": 2.0}).status_code == 401
        assert client.post("/home/geocoding", json={"enabled": True}).status_code == 401
        assert client.get("/geocode/search?q=x").status_code == 401
