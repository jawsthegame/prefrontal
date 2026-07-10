"""Tests for the operator admin surface and token->user auth resolution."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

OP_TOKEN = "operator-token"
USER_TOKEN = "user-token"


@pytest.fixture()
def store():
    """An in-memory store with one operator and one ordinary user."""
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "op", display_name="Operator", token=OP_TOKEN, is_operator=True)
    provision_user(s, "sam", display_name="Sam", token=USER_TOKEN, is_operator=False)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        yield c


def test_admin_requires_operator(client):
    """A non-operator user is forbidden from the admin surface (403)."""
    resp = client.get("/admin/users", headers={"X-Prefrontal-Token": USER_TOKEN})
    assert resp.status_code == 403


def test_admin_lists_users_without_tokens(client):
    """The operator can list users; tokens are never returned."""
    resp = client.get("/admin/users", headers={"X-Prefrontal-Token": OP_TOKEN})
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert {u["handle"] for u in users} == {"op", "sam"}
    assert all("token" not in u and "token_hash" not in u for u in users)


def test_admin_create_user_returns_token_once(client, store):
    """Creating a user returns the raw token once and the user can then auth."""
    resp = client.post(
        "/admin/users",
        json={"handle": "newbie", "display_name": "Newbie"},
        headers={"X-Prefrontal-Token": OP_TOKEN},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]
    assert token
    # The new user is real and seeded (default coaching state present).
    new = store.get_user("newbie")
    assert new is not None
    assert store.scoped(new["id"]).get_state("time_estimation_bias") == "1.4"
    # And its token authenticates a data request.
    ok = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it"},
        headers={"X-Prefrontal-Token": token},
    )
    assert ok.status_code == 201


def test_admin_create_duplicate_handle_conflicts(client):
    """Re-creating an existing handle is a 409."""
    resp = client.post(
        "/admin/users",
        json={"handle": "sam"},
        headers={"X-Prefrontal-Token": OP_TOKEN},
    )
    assert resp.status_code == 409


def test_admin_rotate_invalidates_old_token(client):
    """Rotating a token makes the old one stop resolving and issues a new one."""
    resp = client.post(
        "/admin/users/sam/rotate", headers={"X-Prefrontal-Token": OP_TOKEN}
    )
    assert resp.status_code == 200
    new_token = resp.json()["token"]
    assert new_token != USER_TOKEN
    # Old token no longer works …
    old = client.get("/todos", headers={"X-Prefrontal-Token": USER_TOKEN})
    assert old.status_code == 401
    # … new token does.
    fresh = client.get("/todos", headers={"X-Prefrontal-Token": new_token})
    assert fresh.status_code == 200


def test_admin_disable_blocks_access(client):
    """Disabling a user makes their token stop resolving."""
    resp = client.post(
        "/admin/users/sam/disable", headers={"X-Prefrontal-Token": OP_TOKEN}
    )
    assert resp.status_code == 200
    blocked = client.get("/todos", headers={"X-Prefrontal-Token": USER_TOKEN})
    assert blocked.status_code == 401


def test_request_is_scoped_to_resolved_user(client, store):
    """A todo created under one token is invisible to another user's token."""
    client.post(
        "/todos",
        json={"title": "sam's todo", "estimate_minutes": 5},
        headers={"X-Prefrontal-Token": USER_TOKEN},
    )
    sam_view = client.get("/todos", headers={"X-Prefrontal-Token": USER_TOKEN}).json()
    op_view = client.get("/todos", headers={"X-Prefrontal-Token": OP_TOKEN}).json()
    assert [t["title"] for t in sam_view["todos"]] == ["sam's todo"]
    assert op_view["todos"] == []


def test_admin_lists_households_with_members(client, store):
    """The operator sees each household and who's wired into it."""
    hid = store.create_household("The Kims")
    store.set_user_household("sam", hid)
    resp = client.get("/admin/households", headers={"X-Prefrontal-Token": OP_TOKEN})
    assert resp.status_code == 200
    households = resp.json()["households"]
    assert len(households) == 1
    hh = households[0]
    assert hh["id"] == hid
    assert hh["name"] == "The Kims"
    assert [m["handle"] for m in hh["members"]] == ["sam"]


def test_admin_households_requires_operator(client):
    """A non-operator is forbidden (403); a bad code is unauthorized (401)."""
    forbidden = client.get(
        "/admin/households", headers={"X-Prefrontal-Token": USER_TOKEN}
    )
    assert forbidden.status_code == 403
    unauth = client.get("/admin/households", headers={"X-Prefrontal-Token": "nope"})
    assert unauth.status_code == 401


def test_admin_page_is_served_unauthenticated(client):
    """The /admin shell is a self-contained page (auth happens client-side)."""
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Admin" in resp.text


def test_bootstrap_secret_maps_to_operator(store):
    """The legacy webhook secret resolves to the operator user as a bootstrap."""
    app = create_app(store=store, settings=Settings(webhook_secret="boot-secret"))
    with TestClient(app) as c:
        resp = c.get("/admin/users", headers={"X-Prefrontal-Token": "boot-secret"})
    assert resp.status_code == 200


def test_outing_check_returns_delivery_fields(client):
    """The check response carries per-user delivery routing for n8n."""
    # Sam has no delivery target set, so the routing block reports unconfigured.
    client.post(
        "/webhooks/outing/start",
        json={"intention": "coffee", "time_window_minutes": 15},
        headers={"X-Prefrontal-Token": USER_TOKEN},
    )
    resp = client.post(
        "/webhooks/outing/check", json={}, headers={"X-Prefrontal-Token": USER_TOKEN}
    )
    assert resp.status_code == 200
    active = resp.json()["active"]
    assert len(active) == 1
    assert "delivery" in active[0]
    assert active[0]["delivery_configured"] is False  # no target set


def test_outing_check_reports_delivery_configured_when_target_set(client, store):
    """With a delivery target set, the routing block flips delivery_configured True."""
    sam_id = store.get_user("sam")["id"]
    store.scoped(sam_id).set_state("ntfy_topic", "sam-topic", source="explicit")
    client.post(
        "/webhooks/outing/start",
        json={"intention": "coffee", "time_window_minutes": 15},
        headers={"X-Prefrontal-Token": USER_TOKEN},
    )
    resp = client.post(
        "/webhooks/outing/check", json={}, headers={"X-Prefrontal-Token": USER_TOKEN}
    )
    active = resp.json()["active"]
    assert active[0]["delivery_configured"] is True
