"""Tests for the FastAPI webhook listener.

Uses FastAPI's TestClient with an injected in-memory MemoryStore and a settings
object that enables shared-secret auth, so we can exercise the auth gate and the
shortcut -> episode mapping end to end without touching disk or the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app

SECRET = "test-secret"


@pytest.fixture()
def store():
    """An in-memory, schema-initialized store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    """A TestClient wired to the injected store with auth enabled."""
    settings = Settings(webhook_secret=SECRET)
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        yield c


def test_health_needs_no_auth(client):
    """The health probe responds 200 without a token."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_profile_requires_auth(client):
    """The profile endpoint is auth-guarded."""
    assert client.get("/profile").status_code == 401


def test_profile_returns_markdown(client):
    """With a valid token, /profile returns the behavioral profile as text."""
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    assert "Behavioral profile" in resp.text


def test_shortcut_rejects_missing_token(client):
    """A shortcut POST without the shared secret is rejected with 401."""
    resp = client.post("/webhooks/shortcut", json={"action": "made_it"})
    assert resp.status_code == 401


def test_shortcut_rejects_wrong_token(client):
    """A wrong shared secret is rejected with 401."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it"},
        headers={"X-Prefrontal-Token": "nope"},
    )
    assert resp.status_code == 401


def test_made_it_creates_success_episode(client, store):
    """'made_it' maps to a success episode and persists to the store."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it", "episode_type": "departure", "channel": "notification"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["outcome"] == "success"
    assert body["n8n_delivered"] is False  # n8n disabled by default

    ep = store.get_episode(body["episode_id"])
    assert ep["episode_type"] == "departure"
    assert ep["outcome"] == "success"
    assert ep["acknowledged"] == 1  # one-tap implies acknowledgement


def test_missed_it_creates_miss_episode(client, store):
    """'missed_it' maps to a miss episode."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "missed_it", "episode_type": "reminder"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    assert resp.json()["outcome"] == "miss"


def test_log_action_requires_outcome(client):
    """action='log' without an explicit outcome is a 422."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "log", "episode_type": "task"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422


def test_invalid_episode_type_is_rejected(client):
    """Pydantic rejects an episode_type outside the allowed set."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it", "episode_type": "bogus"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422


def test_n8n_inbound_classifies_event(client):
    """The inbound n8n route echoes a routing decision (handled=False for now)."""
    resp = client.post(
        "/webhooks/n8n",
        json={"event": "mail.received", "id": 7},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["event"] == "mail.received"
    assert body["handled"] is False


def test_auth_disabled_allows_no_token(store):
    """With no secret configured, requests succeed without a token."""
    app = create_app(store=store, settings=Settings(webhook_secret=""))
    with TestClient(app) as c:
        resp = c.post("/webhooks/shortcut", json={"action": "made_it"})
    assert resp.status_code == 201
