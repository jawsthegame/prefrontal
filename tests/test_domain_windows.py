"""Per-life-domain "domain hours" — the editable work/life guardrail + its API.

Covers the ``GET/POST /schedule/domain-windows`` HTTP surface (inherited defaults,
partial writes, clearing an override, validation) and the behavioural payoff: an
override actually changes the band a todo in that domain resolves to
(:func:`prefrontal.scheduling.resolve_window`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.focus_balance import FOCUS_DOMAINS
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import (
    STATE_WINDOW_PREFIX,
    resolve_window,
    window_config_for,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "domain-secret"


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


def test_get_returns_all_domains_with_inherited_defaults(client) -> None:
    body = client.get("/schedule/domain-windows", headers=_auth()).json()
    assert set(body["domains"]) == set(FOCUS_DOMAINS)
    # Nothing configured yet: every domain reports its inherited band, not an override.
    assert all(d["configured"] is False for d in body["domains"].values())
    # "work" inherits the shared category default (09:00–17:00); a domain with no
    # category default falls back to the global default window (06:00–22:00).
    assert body["domains"]["work"] == {"configured": False, "start": "09:00", "end": "17:00"}
    assert body["domains"]["kids"] == {"configured": False, "start": "06:00", "end": "22:00"}


def test_post_round_trips_and_marks_configured(client, store) -> None:
    resp = client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": True, "start": "08:30", "end": "16:30"}}},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["domains"]["work"] == {"configured": True, "start": "08:30", "end": "16:30"}
    # Persisted as the same todo_window override resolve_window already honours.
    entry = store.all_state()[f"{STATE_WINDOW_PREFIX}work"]
    assert entry["value"] == "08:30-16:30"
    assert entry["source"] == "explicit"


def test_post_is_a_partial_merge(client) -> None:
    client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": True, "start": "08:00", "end": "16:00"}}},
        headers=_auth(),
    )
    # A second write naming only "personal" leaves "work" untouched.
    body = client.post(
        "/schedule/domain-windows",
        json={"domains": {"personal": {"configured": True, "start": "18:00", "end": "22:00"}}},
        headers=_auth(),
    ).json()
    assert body["domains"]["work"] == {"configured": True, "start": "08:00", "end": "16:00"}
    assert body["domains"]["personal"] == {"configured": True, "start": "18:00", "end": "22:00"}


def test_clearing_reverts_to_the_inherited_default(client, store) -> None:
    client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": True, "start": "08:30", "end": "16:30"}}},
        headers=_auth(),
    )
    body = client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": False, "start": "08:30", "end": "16:30"}}},
        headers=_auth(),
    ).json()
    # Back to the inherited default, and the override key is gone from state.
    assert body["domains"]["work"] == {"configured": False, "start": "09:00", "end": "17:00"}
    assert f"{STATE_WINDOW_PREFIX}work" not in store.all_state()


def test_post_rejects_end_before_start_and_unknown_domain(client) -> None:
    bad_band = client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": True, "start": "17:00", "end": "09:00"}}},
        headers=_auth(),
    )
    assert bad_band.status_code == 422
    unknown = client.post(
        "/schedule/domain-windows",
        json={"domains": {"bogus": {"configured": True, "start": "09:00", "end": "17:00"}}},
        headers=_auth(),
    )
    assert unknown.status_code == 422


def test_override_changes_the_resolved_window(client, store) -> None:
    """The behavioural payoff: a saved override moves the band a todo resolves to."""
    todo = {"domain": "work"}
    settings = Settings(webhook_secret=SECRET)
    # Before: the inherited work default (the shared category window, 09:00–17:00).
    assert resolve_window(todo, window_config_for(settings, store)) == (9 * 60, 17 * 60)
    client.post(
        "/schedule/domain-windows",
        json={"domains": {"work": {"configured": True, "start": "10:00", "end": "15:00"}}},
        headers=_auth(),
    )
    # After: the user's override now governs todos in the work domain.
    assert resolve_window(todo, window_config_for(settings, store)) == (10 * 60, 15 * 60)
