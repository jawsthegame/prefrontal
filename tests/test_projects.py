"""Tests for projects — the container that groups todos/commitments/focus sessions.

Covers the suggester (heuristic + LLM + threshold), the store repo (CRUD, domain
write-through, cascade, archive), the HTTP router, and the triage integration
that auto-suggests a project for a triaged item.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.projects import (
    MIN_SUGGEST_CONFIDENCE,
    heuristic_project,
    normalize_project_name,
    suggest_project,
)
from tests.conftest import scoped_default

SECRET = "projects-secret"

_PROJECTS = [
    {"id": 1, "name": "Kitchen Remodel",
     "description": "permits contractor quotes tile cabinets", "domain": "home"},
    {"id": 2, "name": "Q3 Launch",
     "description": "marketing website press release", "domain": "work"},
]


def _offline_ollama() -> OllamaClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_json(text: str) -> OllamaClient:
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))


# --- suggester -------------------------------------------------------------

def test_normalize_project_name_collapses_whitespace():
    assert normalize_project_name("  Kitchen   Remodel \n") == "Kitchen Remodel"


def test_heuristic_matches_by_description_tokens():
    pid, score = heuristic_project("Get tile samples from the contractor", None, _PROJECTS)
    assert pid == 1 and score > 0


def test_heuristic_returns_none_when_nothing_overlaps():
    pid, score = heuristic_project("Buy milk", None, _PROJECTS)
    assert pid is None and score == 0.0


def test_suggest_none_when_no_projects():
    assert suggest_project("anything", None, [], client=_ollama_json('{"project_id":1}')) is None


def test_suggest_uses_llm_when_confident():
    client = _ollama_json('{"project_id": 2, "confidence": 0.95}')
    assert suggest_project("Draft the press release", None, _PROJECTS, client=client) == 2


def test_suggest_drops_low_confidence():
    client = _ollama_json('{"project_id": 2, "confidence": 0.2}')
    assert suggest_project("Draft the press release", None, _PROJECTS, client=client) is None


def test_suggest_rejects_unknown_project_id():
    client = _ollama_json('{"project_id": 999, "confidence": 0.99}')
    assert suggest_project("whatever", None, _PROJECTS, client=client) is None


def test_suggest_falls_back_to_heuristic_when_model_down():
    # Offline model -> heuristic; a strongly-overlapping item still matches.
    pid = suggest_project(
        "tile cabinets permits contractor quotes", None, _PROJECTS,
        client=_offline_ollama(),
    )
    assert pid == 1


def test_min_confidence_is_reasonable():
    assert 0.5 <= MIN_SUGGEST_CONFIDENCE <= 0.8


# --- store -----------------------------------------------------------------

@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_create_and_get_project(store):
    pid = store.add_project("Kitchen Remodel", "home", description="tile")
    p = store.get_project(pid)
    assert p["name"] == "Kitchen Remodel" and p["domain"] == "home" and p["status"] == "active"


def test_active_projects_accessor_shape(store):
    store.add_project("Kitchen Remodel", "home", description="tile")
    rows = store.active_projects()
    assert set(rows[0]) == {"id", "name", "description", "domain"}


def test_name_in_use_is_case_insensitive_and_excludes_self(store):
    pid = store.add_project("Kitchen Remodel", "home")
    assert store.project_name_in_use("kitchen remodel") is True
    assert store.project_name_in_use("Kitchen Remodel", exclude_id=pid) is False


def test_assign_writes_project_domain_through(store):
    pid = store.add_project("Kitchen Remodel", "home")
    tid = store.add_todo("Call contractor", domain="work")
    assert store.set_todo_project(tid, pid) is True
    assert store.get_todo(tid)["domain"] == "home"


def test_clearing_project_keeps_domain(store):
    pid = store.add_project("Kitchen Remodel", "home")
    tid = store.add_todo("Call contractor", domain="work")
    store.set_todo_project(tid, pid)
    assert store.set_todo_project(tid, None) is True
    t = store.get_todo(tid)
    assert t["project_id"] is None and t["domain"] == "home"


def test_assign_unknown_project_is_rejected(store):
    tid = store.add_todo("x")
    assert store.set_todo_project(tid, 424242) is False


def test_domain_change_cascades_to_members(store):
    pid = store.add_project("Kitchen Remodel", "home")
    tid = store.add_todo("Call contractor")
    cid, _ = store.upsert_commitment(title="Site visit", start_at="2026-07-20 10:00:00")
    store.set_todo_project(tid, pid)
    store.set_commitment_project(cid, pid)
    store.update_project(pid, domain="work")
    assert store.get_todo(tid)["domain"] == "work"
    assert store.get_commitment(cid)["domain"] == "work"


def test_commitment_project_survives_calendar_resync(store):
    pid = store.add_project("Roofing", "home")
    cid, _ = store.upsert_commitment(
        title="Site visit", start_at="2026-07-20 10:00:00", external_id="evt-1"
    )
    store.set_commitment_project(cid, pid)
    # Re-sync the same event (new title/time) — project_id is a user field, kept.
    store.upsert_commitment(
        title="Site visit (moved)", start_at="2026-07-20 11:00:00", external_id="evt-1"
    )
    assert store.get_commitment(cid)["project_id"] == pid


def test_focus_session_carries_project(store):
    pid = store.add_project("Kitchen Remodel", "home")
    sid = store.start_focus_session("measure cabinets", project_id=pid)
    assert store.get_focus_session(sid)["project_id"] == pid


def test_archive_frees_the_name(store):
    pid = store.add_project("Kitchen Remodel", "home")
    assert store.archive_project(pid) is True
    assert store.project_name_in_use("Kitchen Remodel") is False
    assert store.add_project("Kitchen Remodel", "home") != pid


# --- HTTP API --------------------------------------------------------------

@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store_open):
    from prefrontal.webhooks.app import create_app

    app = create_app(
        store=store_open, settings=Settings(webhook_secret=SECRET), ollama=_offline_ollama()
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_api_create_list_get(client):
    r = client.post(
        "/projects",
        json={"name": "Kitchen Remodel", "domain": "home", "description": "tile"},
        headers=_auth(),
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get("/projects", headers=_auth()).json()["projects"][0]["id"] == pid
    detail = client.get(f"/projects/{pid}", headers=_auth()).json()
    assert detail["project"]["name"] == "Kitchen Remodel"
    assert detail["todos"] == [] and detail["commitments"] == []


def test_api_duplicate_name_conflicts(client):
    client.post("/projects", json={"name": "Kitchen", "domain": "home"}, headers=_auth())
    r = client.post("/projects", json={"name": "Kitchen", "domain": "home"}, headers=_auth())
    assert r.status_code == 409


def test_api_freetext_domain_allowed(client):
    # Domains are lenient (like todos/trips): an unknown value is kept as free text.
    r = client.post("/projects", json={"name": "X", "domain": "sabbatical"}, headers=_auth())
    assert r.status_code == 201 and r.json()["domain"] == "sabbatical"


def test_api_blank_domain_422(client):
    r = client.post("/projects", json={"name": "X", "domain": "   "}, headers=_auth())
    assert r.status_code == 422


def test_api_assign_todo_and_cascade(client, store_open):
    pid = client.post(
        "/projects", json={"name": "Kitchen", "domain": "home"}, headers=_auth()
    ).json()["id"]
    tid = client.post(
        "/todos", json={"title": "Call contractor"}, headers=_auth()
    ).json()["todo_id"]
    r = client.post(f"/todos/{tid}/project", json={"project_id": pid}, headers=_auth())
    assert r.status_code == 200
    assert store_open.get_todo(tid)["domain"] == "home"
    # PATCH domain cascades to the assigned todo
    client.patch(f"/projects/{pid}", json={"domain": "work"}, headers=_auth())
    assert store_open.get_todo(tid)["domain"] == "work"


def test_api_assign_unknown_project_422(client):
    tid = client.post("/todos", json={"title": "x"}, headers=_auth()).json()["todo_id"]
    r = client.post(f"/todos/{tid}/project", json={"project_id": 999}, headers=_auth())
    assert r.status_code == 422


def test_api_assign_missing_todo_404(client):
    pid = client.post(
        "/projects", json={"name": "K", "domain": "home"}, headers=_auth()
    ).json()["id"]
    r = client.post("/todos/999/project", json={"project_id": pid}, headers=_auth())
    assert r.status_code == 404


def test_api_archive(client):
    pid = client.post(
        "/projects", json={"name": "K", "domain": "home"}, headers=_auth()
    ).json()["id"]
    assert client.post(f"/projects/{pid}/archive", headers=_auth()).status_code == 200
    assert client.get("/projects", headers=_auth()).json()["projects"] == []
    assert len(client.get("/projects?include_archived=1", headers=_auth()).json()["projects"]) == 1


# --- triage integration ----------------------------------------------------

def test_triage_todo_gets_suggested_project(store):
    """A triaged action-item is auto-filed into a confidently-matched project."""
    from datetime import date

    from prefrontal.triage import Signal, TriageDecision, apply

    store.add_project("Kitchen Remodel", "home", description="tile cabinets contractor permits")
    client = _ollama_json('{"project_id": 1, "confidence": 0.9}')
    signal = Signal(source="manual", title="Email the contractor about tile", body="need quotes")
    decision = TriageDecision(
        kind="action", urgency="normal", route="todo", reason="test",
        confidence=1.0, source="heuristic",
    )
    apply(signal, decision, store, client=client, today=date(2026, 7, 10))
    todos = store.open_todos()
    assert todos and todos[0]["project_id"] == 1
