"""The ``GET/POST /schedule/location-settings`` HTTP surface.

Defaults, partial writes, persistence as explicit coaching keys, the shared
``home_radius_m`` key, and bounds validation. The structural shape is pinned
separately by ``tests/test_contract_location_settings.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "loc-secret"


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


def test_get_defaults(client) -> None:
    body = client.get("/schedule/location-settings", headers=_auth()).json()
    assert body == {
        "home_radius_m": 150.0,
        "geofence_radius_m": 120.0,
        "post_interval_s": 300,
        "visits_enabled": True,
    }


def test_post_partial_write_leaves_other_fields(client) -> None:
    # Set only the geofence radius; the rest keep their defaults.
    body = client.post(
        "/schedule/location-settings",
        json={"geofence_radius_m": 200},
        headers=_auth(),
    ).json()
    assert body["geofence_radius_m"] == 200.0
    assert body["home_radius_m"] == 150.0
    assert body["post_interval_s"] == 300
    assert body["visits_enabled"] is True

    # A second write naming only visits leaves the radius untouched.
    body = client.post(
        "/schedule/location-settings",
        json={"visits_enabled": False},
        headers=_auth(),
    ).json()
    assert body["visits_enabled"] is False
    assert body["geofence_radius_m"] == 200.0


def test_home_radius_is_the_shared_key(client, store) -> None:
    # Writing home_radius_m here is the same key outing gating / trips read.
    client.post(
        "/schedule/location-settings", json={"home_radius_m": 250}, headers=_auth()
    )
    assert store.get_float("home_radius_m", 0.0) == 250.0
    entry = store.all_state()["home_radius_m"]
    assert entry["source"] == "explicit"


def test_post_persists_as_explicit(client, store) -> None:
    client.post(
        "/schedule/location-settings",
        json={"post_interval_s": 600, "visits_enabled": False},
        headers=_auth(),
    )
    assert store.get_float("location_post_interval_s", 0.0) == 600.0
    assert store.get_bool("location_visits_enabled", True) is False


def test_post_rejects_out_of_range(client) -> None:
    # geofence radius below the 50 m floor, post interval above the 3600 s ceiling.
    assert client.post(
        "/schedule/location-settings", json={"geofence_radius_m": 10}, headers=_auth()
    ).status_code == 422
    assert client.post(
        "/schedule/location-settings", json={"post_interval_s": 99999}, headers=_auth()
    ).status_code == 422
