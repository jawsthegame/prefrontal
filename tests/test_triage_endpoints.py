"""Tests for the triage HTTP surface — /webhooks/n8n, POST /triage, GET /triage/recent."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

SECRET = "tok"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "tester", token=SECRET)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    # Pure-heuristic triage (no model) keeps these deterministic.
    with TestClient(create_app(store=store, settings=Settings(triage_use_llm=False))) as c:
        yield c


def _h():
    return {"X-Prefrontal-Token": SECRET}


def test_triage_endpoint_requires_auth(client):
    assert client.post("/triage", json={"title": "hi"}).status_code == 401
    assert client.get("/triage/recent").status_code == 401


def test_post_triage_routes_a_dated_commitment(client):
    resp = client.post(
        "/triage",
        json={"source": "mail", "title": "Dentist appointment confirmed for Tuesday 3pm"},
        headers=_h(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["kind"] == "commitment"
    assert body["routed_ref"].startswith("commitment:")


def test_post_triage_drops_noise(client):
    resp = client.post(
        "/triage",
        json={"source": "mail", "title": "20% off — our weekly newsletter",
              "sender": "newsletter@shop.com"},
        headers=_h(),
    )
    body = resp.json()
    assert body["decision"]["kind"] == "noise"
    assert body["routed_ref"] is None  # dropped, nothing created


def test_recent_lists_decisions_newest_first(client):
    client.post("/triage", json={"title": "please pay invoice today"}, headers=_h())
    client.post(
        "/triage", json={"title": "our newsletter", "sender": "noreply@x.com"}, headers=_h()
    )
    recent = client.get("/triage/recent", headers=_h()).json()["decisions"]
    assert len(recent) == 2
    assert recent[0]["title"] == "our newsletter"  # newest first
    assert recent[0]["reason"]  # explainable


def test_dashboard_has_a_triage_panel():
    """The dashboard ships a recent-triage card that reads /triage/recent."""
    from prefrontal.webhooks._common import DASHBOARD_HTML

    assert 'id="c-triagelog"' in DASHBOARD_HTML
    assert "/triage/recent" in DASHBOARD_HTML


def test_post_triage_is_idempotent_on_external_id(client):
    payload = {"source": "mail", "title": "pay invoice today", "external_id": "gmail-42"}
    first = client.post("/triage", json=payload, headers=_h()).json()
    second = client.post("/triage", json=payload, headers=_h()).json()
    assert second["idempotent"] is True
    assert second["triage_id"] == first["triage_id"]
    # Only one row and one todo despite two posts.
    assert len(client.get("/triage/recent", headers=_h()).json()["decisions"]) == 1
    assert len(client.get("/todos", headers=_h()).json()["todos"]) == 1
