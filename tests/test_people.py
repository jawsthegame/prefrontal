"""Tests for the people queue — names in ingested items, identified + categorized.

Covers the pure extractor (:mod:`prefrontal.people`), the roster + mention-queue
repo, the HTTP router (queue → identify/dismiss, roster CRUD, on-demand extract),
the triage ingestion hook that fills the queue and boosts priority for a known
important person, and the "Key people" profile section (learning).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.people import (
    describe_mention,
    describe_person,
    enqueue_mentions,
    extract_names,
    name_key,
    normalize_name,
    priority_boost,
    roster_profile_lines,
)
from tests.conftest import scoped_default

SECRET = "people-secret"


def _offline_ollama() -> OllamaClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


# --- pure extraction --------------------------------------------------------


def test_normalize_and_key():
    assert normalize_name("  Dana   Ruiz \n") == "Dana Ruiz"
    assert name_key("  Dana   RUIZ ") == "dana ruiz"


def test_extract_titlecase_bigram():
    assert extract_names("the budget from Dana Ruiz is late") == ["Dana Ruiz"]


def test_extract_lone_name_needs_a_cue():
    # A lone capitalized word with a cue is kept; without one it's dropped.
    assert extract_names("call Sam before noon") == ["Sam"]
    assert extract_names("Sam is a great name for a dog") == []


def test_extract_strips_sentence_initial_verb():
    # A greedy bigram that begins with a cue/verb is peeled back to the name.
    assert extract_names("Ping Sam and Dana Ruiz again") == ["Sam", "Dana Ruiz"]
    assert extract_names("Email Sam the numbers") == ["Sam"]


def test_extract_honorific_and_possessive():
    assert extract_names("follow up with Dr. Lee tomorrow") == ["Lee"]
    assert "Mom" in extract_names("Mom's cardiology appt")


def test_extract_drops_stopwords_and_places():
    assert extract_names("Please review the sale by Friday") == []
    assert extract_names("New York trip next week") == []
    assert extract_names("Best Regards") == []


def test_extract_dedupes_case_insensitively():
    assert extract_names("Sam emailed. Later, call Sam again.") == ["Sam"]


def test_extract_cue_does_not_cross_a_period():
    # A sentence-ending word must not cue the next sentence's first capital word.
    assert extract_names("I emailed. Later is fine.") == []
    assert extract_names("w/ Dana at noon") == ["Dana"]  # slash cue still works


# --- repo: roster + queue ---------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def test_add_and_find_person_by_name_and_alias(store):
    pid = store.add_person(
        name="Dana Ruiz", name_key=name_key("Dana Ruiz"),
        relationship="coworker", importance=2, aliases=["Dana"],
    )
    assert store.find_person("dana ruiz")["id"] == pid
    assert store.find_person("dana")["id"] == pid  # alias match
    assert store.find_person("nobody") is None


def test_touch_person_accumulates_recurrence(store):
    pid = store.add_person(name="Sam", name_key=name_key("Sam"))
    store.touch_person(pid)
    store.touch_person(pid)
    person = store.get_person(pid)
    assert person["mention_count"] == 2
    assert person["first_seen"] is not None and person["last_seen"] is not None


def test_pending_mention_dedupes_by_name(store):
    first = store.add_person_mention(name="Sam", source="mail")
    again = store.add_person_mention(name="sam", source="calendar")  # same key
    assert first > 0
    assert again == 0  # de-duped by the partial unique index
    assert len(store.list_person_mentions("pending")) == 1


def test_identify_and_dismiss_are_idempotent(store):
    mid = store.add_person_mention(name="Sam", source="mail")
    pid = store.add_person(name="Sam", name_key=name_key("Sam"))
    assert store.identify_person_mention(mid, pid) is True
    assert store.identify_person_mention(mid, pid) is False  # already resolved
    assert store.count_pending_mentions() == 0

    mid2 = store.add_person_mention(name="Dana", source="mail")
    assert store.dismiss_person_mention(mid2) is True
    assert store.dismiss_person_mention(mid2) is False


# --- orchestration: enqueue + boost -----------------------------------------


def test_enqueue_queues_unknown_touches_known(store):
    pid = store.add_person(name="Sam", name_key=name_key("Sam"), importance=1)
    result = enqueue_mentions(
        store, text="call with Sam and Dana Ruiz", source="mail", ref="todo:5"
    )
    assert result["known"] == [pid]
    assert len(result["queued"]) == 1  # only Dana Ruiz queued
    assert store.get_person(pid)["mention_count"] == 1
    queued = store.list_person_mentions("pending")
    assert [m["name"] for m in queued] == ["Dana Ruiz"]
    assert queued[0]["ref"] == "todo:5"


def test_priority_boost_scales_with_importance(store):
    store.add_person(name="Sam", name_key=name_key("Sam"), importance=3)
    store.add_person(name="Dana", name_key=name_key("Dana"), importance=2)
    store.add_person(name="Lee", name_key=name_key("Lee"), importance=1)
    assert priority_boost(store, "email Sam the numbers") == 2
    assert priority_boost(store, "email Dana the numbers") == 1
    assert priority_boost(store, "email Lee the numbers") == 0
    assert priority_boost(store, "email Jordan the numbers") == 0  # unknown


def test_archived_person_neither_matched_nor_boosts(store):
    pid = store.add_person(name="Sam", name_key=name_key("Sam"), importance=3, status="archived")
    # find_person still returns the row, but enqueue/boost only act on active people.
    assert priority_boost(store, "call Sam") == 0
    result = enqueue_mentions(store, text="call Sam", source="mail")
    assert result["queued"]  # archived → treated as unknown, re-queued for review
    assert pid not in result["known"]


# --- surface helpers --------------------------------------------------------


def test_describe_helpers():
    person = {"name": "Dana Ruiz", "relationship": "coworker", "importance": 2, "mention_count": 4}
    assert describe_person(person) == "Dana Ruiz — coworker · high · seen 4×"
    mention = {"name": "Sam", "source": "mail", "context": "call with Sam"}
    assert describe_mention(mention) == 'Sam — from mail: "call with Sam"'


def test_roster_profile_lines_only_notable():
    people = [
        {"name": "Sam", "relationship": "family", "importance": 3, "mention_count": 9,
         "status": "active", "notes": "spouse"},
        {"name": "Dana", "relationship": "coworker", "importance": 2, "mention_count": 2,
         "status": "active", "notes": None},
        {"name": "Lee", "relationship": "friend", "importance": 1, "mention_count": 50,
         "status": "active"},
    ]
    lines = roster_profile_lines(people)
    assert len(lines) == 2  # Lee (importance 1) excluded
    assert "**Sam**" in lines[0] and "spouse" in lines[0]  # highest importance first
    assert "**Dana**" in lines[1]


# --- HTTP router ------------------------------------------------------------


@pytest.fixture()
def client(store):
    from prefrontal.webhooks.app import create_app

    app = create_app(
        store=store, settings=Settings(webhook_secret=SECRET), ollama=_offline_ollama()
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_http_queue_identify_new_person(client):
    client.post(
        "/people/extract", json={"text": "coffee with Priya Patel"}, headers=_auth()
    )
    queue = client.get("/people/queue", headers=_auth()).json()["mentions"]
    assert [m["name"] for m in queue] == ["Priya Patel"]
    mid = queue[0]["id"]
    r = client.post(
        f"/people/mentions/{mid}/identify",
        json={"relationship": "friend", "importance": 2},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["person"]["name"] == "Priya Patel"
    assert r.json()["person"]["relationship"] == "friend"
    # queue drained; roster now holds her.
    assert client.get("/people/queue", headers=_auth()).json()["mentions"] == []
    assert [p["name"] for p in client.get("/people", headers=_auth()).json()["people"]] == [
        "Priya Patel"
    ]


def test_http_extract_returns_integer_counts(client):
    r = client.post(
        "/people/extract", json={"text": "email Sam and Priya Patel"}, headers=_auth()
    )
    body = r.json()
    assert body["queued"] == 2 and body["known"] == 0  # counts (ints), not lists
    assert isinstance(body["names"], list)


def test_http_patch_null_aliases_clears_not_corrupts(client):
    pid = client.post(
        "/people", json={"name": "Dana", "aliases": ["D"]}, headers=_auth()
    ).json()["id"]
    r = client.patch(f"/people/{pid}", json={"aliases": None}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["aliases"] == []
    # find_person still iterates aliases without crashing.
    assert client.get("/people", headers=_auth()).json()["people"][0]["aliases"] == []


def test_http_identify_links_existing_and_learns_alias(client):
    pid = client.post(
        "/people", json={"name": "Dana Ruiz", "importance": 2}, headers=_auth()
    ).json()["id"]
    client.post("/people/extract", json={"text": "ping Dana about it"}, headers=_auth())
    mid = client.get("/people/queue", headers=_auth()).json()["mentions"][0]["id"]
    r = client.post(
        f"/people/mentions/{mid}/identify", json={"person_id": pid}, headers=_auth()
    )
    assert r.status_code == 200
    person = client.get("/people", headers=_auth()).json()["people"][0]
    assert "Dana" in person["aliases"]  # the mentioned spelling is learned as an alias


def test_http_dismiss_and_double_resolve_404(client):
    client.post("/people/extract", json={"text": "call Sam"}, headers=_auth())
    mid = client.get("/people/queue", headers=_auth()).json()["mentions"][0]["id"]
    assert client.post(f"/people/mentions/{mid}/dismiss", headers=_auth()).status_code == 200
    # already resolved → 404 on any further action
    assert client.post(f"/people/mentions/{mid}/dismiss", headers=_auth()).status_code == 404
    assert (
        client.post(f"/people/mentions/{mid}/identify", json={}, headers=_auth()).status_code
        == 404
    )


def test_http_roster_crud_and_validation(client):
    r = client.post(
        "/people", json={"name": "Sam", "relationship": "family", "importance": 3},
        headers=_auth(),
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    # duplicate name → 409
    assert client.post("/people", json={"name": "sam"}, headers=_auth()).status_code == 409
    # bad relationship → 422
    bad = client.post("/people", json={"name": "X", "relationship": "nope"}, headers=_auth())
    assert bad.status_code == 422
    # recategorize
    r = client.patch(f"/people/{pid}", json={"importance": 1, "notes": "brother"}, headers=_auth())
    assert r.json()["importance"] == 1 and r.json()["notes"] == "brother"
    # archive drops from the active list
    client.post(f"/people/{pid}/archive", headers=_auth())
    assert client.get("/people", headers=_auth()).json()["people"] == []
    assert len(client.get("/people?status=all", headers=_auth()).json()["people"]) == 1
    assert client.patch("/people/999", json={"notes": "x"}, headers=_auth()).status_code == 404


# --- triage integration -----------------------------------------------------


def test_triage_populates_queue_and_boosts_priority(client, store):
    # A known, top-importance person…
    store.add_person(name="Sam", name_key=name_key("Sam"), relationship="family", importance=3)
    # …named in an actionable signal alongside a stranger.
    r = client.post(
        "/triage",
        json={"title": "Please send Sam the Dana Ruiz report", "source": "manual"},
        headers=_auth(),
    )
    assert r.status_code == 200
    ref = r.json()["routed_ref"]
    assert ref.startswith("todo:")
    # The stranger is queued for review; the known person is not.
    queued = [m["name"] for m in client.get("/people/queue", headers=_auth()).json()["mentions"]]
    assert "Dana Ruiz" in queued and "Sam" not in queued
    # The todo naming a top-importance person got a priority bump above the default (1).
    todo = store.get_todo(int(ref.split(":", 1)[1]))
    assert todo["priority"] >= 3


def test_triage_drop_does_not_queue_names(client):
    # A newsletter is dropped as noise — its sender name should not enter the queue.
    client.post(
        "/triage",
        json={
            "title": "Weekly newsletter from Dana Ruiz",
            "sender": "newsletter@example.com",
            "source": "mail",
        },
        headers=_auth(),
    )
    assert client.get("/people/queue", headers=_auth()).json()["mentions"] == []


# --- summarizer -------------------------------------------------------------


def test_profile_lists_key_people(store):
    from prefrontal.memory.summarizer import build_profile

    store.add_person(
        name="Sam", name_key=name_key("Sam"), relationship="family", importance=3, notes="spouse"
    )
    store.add_person(name="Lee", name_key=name_key("Lee"), relationship="friend", importance=1)
    profile = build_profile(store, modules=[])
    assert "## Key people" in profile
    assert "**Sam**" in profile
    assert "**Lee**" not in profile  # below the importance floor
