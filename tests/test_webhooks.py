"""Tests for the FastAPI webhook listener.

Uses FastAPI's TestClient with an injected in-memory MemoryStore. The store is
multi-tenant: a single user is provisioned and its per-user token is sent in the
``X-Prefrontal-Token`` header, so we exercise token->user resolution and the
shortcut -> episode mapping end to end without touching disk or the network.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

#: The per-user token the fixtures authenticate with. A fixed value is fine in
#: tests (provision_user accepts an explicit token); the auth layer stores only
#: its sha256 and resolves the request to the provisioned user.
SECRET = "test-secret"


@pytest.fixture()
def store():
    """An in-memory store with one provisioned user, kept open for the test.

    The unscoped store is handed to ``create_app``; the app's auth layer scopes
    each request to the user resolved from the ``X-Prefrontal-Token`` header.
    """
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(
        unscoped, "tester", display_name="Tester", token=SECRET, is_operator=True
    )
    try:
        yield unscoped
    finally:
        conn.close()


@pytest.fixture()
def user_store(store):
    """The same store scoped to the provisioned user, for direct per-user calls.

    The app scopes requests itself; this is for assertions/seeding in the test
    body that touch per-user tables directly (which require a bound user).
    """
    return store.scoped(store.get_user("tester")["id"])


@pytest.fixture()
def client(store):
    """A TestClient wired to the injected store; requests carry the user token."""
    settings = Settings()
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
    """With no narrative cached, /profile falls back to the structured profile."""
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    assert "Behavioral profile" in resp.text
    assert resp.headers["X-Profile-Source"] == "uncached"


def test_profile_serves_cached_narrative(client, user_store):
    """Once a narrative is cached, /profile serves the prose, not the structure."""
    from prefrontal.memory.summarizer import build_profile

    user_store.set_profile_cache(
        "Lead with departures: pad estimates 1.4x.",
        source="llm",
        model="qwen2.5:14b",
        structured=build_profile(user_store),  # matches current facts → not stale
    )
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    assert resp.text == "Lead with departures: pad estimates 1.4x."
    assert resp.headers["X-Profile-Source"] == "llm"
    assert resp.headers["X-Profile-Model"] == "qwen2.5:14b"
    assert resp.headers["X-Profile-Generated-At"]
    assert resp.headers["X-Profile-Stale"] == "false"


def test_profile_format_structured_bypasses_cache(client, user_store):
    """?format=structured returns the raw profile even when a narrative is cached."""
    user_store.set_profile_cache(
        "cached prose", source="llm", model="m", structured="# Behavioral profile\n"
    )
    resp = client.get(
        "/profile?format=structured", headers={"X-Prefrontal-Token": SECRET}
    )
    assert resp.status_code == 200
    assert "Behavioral profile" in resp.text
    assert "cached prose" not in resp.text
    assert resp.headers["X-Profile-Source"] == "structured"


def test_profile_stale_header_tracks_facts(client, user_store):
    """Changing an underlying fact flips X-Profile-Stale to true."""
    user_store.set_profile_cache(
        "prose", source="llm", model="m", structured="# Behavioral profile\nstale"
    )
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.headers["X-Profile-Stale"] == "true"  # structured no longer matches


def _mock_ollama(reply: str) -> OllamaClient:
    """An OllamaClient whose /api/generate always returns `reply`."""
    return OllamaClient(
        model="mock-model",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"response": reply})
        ),
    )


def test_profile_refresh_generates_and_caches(store, user_store):
    """?refresh=1 regenerates the narrative via Ollama and persists it."""
    settings = Settings()
    app = create_app(store=store, settings=settings, ollama=_mock_ollama("fresh prose"))
    with TestClient(app) as c:
        resp = c.get("/profile?refresh=1", headers={"X-Prefrontal-Token": SECRET})
        assert resp.status_code == 200
        assert resp.text == "fresh prose"
        assert resp.headers["X-Profile-Source"] == "llm"
    # The regenerated narrative is now cached for subsequent (non-refresh) reads.
    assert user_store.get_profile_cache()["text"] == "fresh prose"


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


def test_made_it_creates_success_episode(client, user_store):
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

    ep = user_store.get_episode(body["episode_id"])
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


def test_default_user_allows_no_token(store):
    """With PREFRONTAL_DEFAULT_USER set, a tokenless request resolves to that user."""
    app = create_app(store=store, settings=Settings(default_user="tester"))
    with TestClient(app) as c:
        resp = c.post("/webhooks/shortcut", json={"action": "made_it"})
    assert resp.status_code == 201


def test_disabled_user_token_is_rejected(store):
    """A disabled user's token no longer resolves (401)."""
    store.set_user_status("tester", "disabled")
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/shortcut",
            json={"action": "made_it"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 401
