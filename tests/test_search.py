"""Tests for global search — the cross-entity dashboard search.

Covers the store repo (:meth:`MemoryStore.search`: substring matching across
todos/commitments/focus sessions/projects, per-user scoping, wildcard escaping,
per-type limit) and the ``GET /search`` HTTP endpoint.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "search-secret"


def _offline_ollama() -> OllamaClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _seed(store: MemoryStore) -> None:
    """Populate one of each searchable entity with distinctive text."""
    store.add_todo("Call the dentist", notes="reschedule the cleaning",
                   category="health", domain="home")
    store.add_todo("Buy tile samples", notes="for the kitchen")
    store.add_project("Kitchen Remodel", "home",
                      description="tile contractor quotes")
    store.upsert_commitment(title="Dentist appointment",
                            start_at="2026-07-15 14:00:00",
                            location="123 Main St", source="manual")
    store.start_focus_session("API refactor sprint",
                              started_at="2026-07-01 09:00:00")


# --- repo ------------------------------------------------------------------

@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_search_matches_title_substring(store):
    _seed(store)
    hits = store.search("tile")
    assert [t["title"] for t in hits["todos"]] == ["Buy tile samples"]
    assert [p["name"] for p in hits["projects"]] == ["Kitchen Remodel"]


def test_search_matches_notes_and_location(store):
    _seed(store)
    # "cleaning" only appears in a todo's notes; "Main" only in a commitment's location.
    assert store.search("cleaning")["todos"][0]["title"] == "Call the dentist"
    assert store.search("Main")["commitments"][0]["title"] == "Dentist appointment"


def test_search_spans_entity_types(store):
    _seed(store)
    hits = store.search("dentist")  # todo title + commitment title
    assert [t["title"] for t in hits["todos"]] == ["Call the dentist"]
    assert [c["title"] for c in hits["commitments"]] == ["Dentist appointment"]


def test_search_is_case_insensitive(store):
    _seed(store)
    assert store.search("API")["focus_sessions"][0]["intended_task"] == "API refactor sprint"
    assert store.search("api")["focus_sessions"][0]["intended_task"] == "API refactor sprint"


def test_blank_query_returns_empty_groups(store):
    _seed(store)
    hits = store.search("   ")
    assert hits == {"todos": [], "commitments": [], "focus_sessions": [], "projects": []}


def test_wildcards_are_literal_not_match_all(store):
    """A ``%`` in the query matches a literal percent, not every row."""
    store.add_todo("Sale: 50% off everything")
    store.add_todo("Unrelated errand")
    hits = store.search("50%")
    assert [t["title"] for t in hits["todos"]] == ["Sale: 50% off everything"]
    # A bare "%" is a literal percent, not SQL match-all: it finds the row that
    # actually contains "%", and not the one that doesn't.
    percent_titles = [t["title"] for t in store.search("%")["todos"]]
    assert percent_titles == ["Sale: 50% off everything"]


def test_limit_caps_results_per_type(store):
    for i in range(5):
        store.add_todo(f"recurring task {i}")
    assert len(store.search("recurring", limit_per_type=2)["todos"]) == 2


def test_hidden_commitments_are_excluded(store):
    cid, _ = store.upsert_commitment(title="Secret meeting",
                                     start_at="2026-07-20 10:00:00", source="manual")
    store.set_commitment_hidden(cid, True)
    assert store.search("Secret")["commitments"] == []


def test_ranking_title_beats_notes(store):
    store.add_todo("Unrelated errand", notes="pick up the tile order")  # match in notes
    store.add_todo("Order bathroom tile")                              # match in title
    ranked = [t["title"] for t in store.search("tile")["todos"]]
    assert ranked == ["Order bathroom tile", "Unrelated errand"]


def test_ranking_prefix_beats_midword(store):
    store.add_todo("Reptile terrarium cleanup")  # "tile" buried mid-word
    store.add_todo("Tile the backsplash")        # title starts with the term
    ranked = [t["title"] for t in store.search("tile")["todos"]]
    assert ranked == ["Tile the backsplash", "Reptile terrarium cleanup"]


def test_ranking_live_item_beats_stale(store):
    open_id = store.add_todo("Renew passport")
    done_id = store.add_todo("Renew passport")
    store.close_todo(done_id, status="done")
    ranked = [t["id"] for t in store.search("passport")["todos"]]
    # Equal text relevance → the still-open todo sorts ahead of the done one.
    assert ranked == [open_id, done_id]


def test_search_is_user_scoped():
    conn = init_db(":memory:")
    try:
        unscoped = MemoryStore(conn)
        alice, _ = provision_user(unscoped, "alice", display_name="Alice")
        bob, _ = provision_user(unscoped, "bob", display_name="Bob")
        unscoped.scoped(alice["id"]).add_todo("Alice's private todo")
        # Bob searches the shared term and sees nothing of Alice's.
        assert unscoped.scoped(bob["id"]).search("private")["todos"] == []
        assert unscoped.scoped(alice["id"]).search("private")["todos"][0]["title"] \
            == "Alice's private todo"
    finally:
        conn.close()


# --- HTTP ------------------------------------------------------------------

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

    _seed(store_open)
    app = create_app(
        store=store_open, settings=Settings(webhook_secret=SECRET), ollama=_offline_ollama()
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_api_search_groups_and_totals(client):
    body = client.get("/search", params={"q": "dentist"}, headers=_auth()).json()
    assert body["query"] == "dentist"
    assert body["total"] == 2
    assert body["results"]["todos"][0]["title"] == "Call the dentist"
    assert body["results"]["commitments"][0]["title"] == "Dentist appointment"


def test_api_blank_query_is_ok_and_empty(client):
    r = client.get("/search", params={"q": "  "}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_api_limit_is_bounded(client):
    # Over the max is a validation error rather than a full-table dump.
    r = client.get("/search", params={"q": "x", "limit": 9999}, headers=_auth())
    assert r.status_code == 422


def test_api_requires_auth(client):
    assert client.get("/search", params={"q": "dentist"}).status_code == 401
