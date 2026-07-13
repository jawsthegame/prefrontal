"""Last-known-location staleness guard (#568).

The `at`-based freshness helpers (`location_age_seconds`, `fresh_location`) and
the `GET /location` age/stale surfacing. Consumers (departure travel-time, outing
gating) call `fresh_location` so a coordinate from hours ago is treated as absent
rather than driving a wrong estimate; a genuinely current fix is unaffected.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import utcnow
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.repos.state import DEFAULT_LOCATION_TTL_SECONDS
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "stale-secret"


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


# -- pure: age + freshness --------------------------------------------------


def test_age_is_none_when_unset(store) -> None:
    assert store.location_age_seconds() is None
    assert store.fresh_location() is None


def test_age_measured_from_the_fix_timestamp(store) -> None:
    store.set_location(37.77, -122.41)
    # Inject a clock 100 s ahead of the just-recorded fix.
    age = store.location_age_seconds(now=utcnow() + timedelta(seconds=100))
    assert age == pytest.approx(100, abs=5)


def test_fresh_within_window_stale_beyond(store) -> None:
    store.set_location(37.77, -122.41)
    # As of ~now the fix is fresh and returned intact.
    fresh = store.fresh_location(now=utcnow())
    assert fresh is not None and fresh["lat"] == pytest.approx(37.77)
    # Two hours later it's past the 1 h default window → treated as absent.
    assert store.fresh_location(now=utcnow() + timedelta(hours=2)) is None


def test_fresh_respects_explicit_and_configured_ttl(store) -> None:
    store.set_location(37.77, -122.41)
    later = utcnow() + timedelta(seconds=90)
    # A tight explicit window makes a 90-s-old fix stale…
    assert store.fresh_location(max_age_seconds=60, now=later) is None
    # …while a generous one keeps it.
    assert store.fresh_location(max_age_seconds=600, now=later) is not None
    # The configured coaching key is the default when no explicit arg is passed.
    store.set_state("location_staleness_seconds", "60", source="explicit")
    assert store.fresh_location(now=later) is None


# -- HTTP: GET /location surfaces freshness ---------------------------------


def test_get_location_unset(client) -> None:
    body = client.get("/location", headers=_auth()).json()
    assert body == {"location": None, "age_seconds": None, "stale": False}


def test_get_location_fresh(client) -> None:
    client.post("/webhooks/location", json={"lat": 37.77, "lon": -122.41}, headers=_auth())
    body = client.get("/location", headers=_auth()).json()
    assert body["location"]["lat"] == pytest.approx(37.77)
    assert body["age_seconds"] is not None and body["age_seconds"] < 60
    assert body["stale"] is False


def test_get_location_stale_flag(client, store) -> None:
    client.post("/webhooks/location", json={"lat": 37.77, "lon": -122.41}, headers=_auth())
    # Back-date the stored fix well beyond the default window.
    store.conn.execute(
        "UPDATE coaching_state SET last_updated = datetime('now', '-2 hours') "
        "WHERE key = 'last_location_lat'"
    )
    store.conn.commit()
    body = client.get("/location", headers=_auth()).json()
    assert body["location"] is not None  # not hidden — the client decides
    assert body["age_seconds"] > DEFAULT_LOCATION_TTL_SECONDS
    assert body["stale"] is True
