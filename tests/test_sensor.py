"""Tests for the LLM-as-sensor (learning §2).

Covers the extractor's grounded JSON parsing + allowlist validation, the honest
no-model fallback, and the propose→confirm→apply loop (candidates land pending
and only reach the store — stamped source='llm_inferred' — on accept).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.sensor import (
    apply_proposal,
    extract_candidates,
    extract_candidates_from_transcript,
    record_candidates,
    render_transcript,
)
from tests.conftest import scoped_default


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _client(reply: str, *, status: int = 200) -> OllamaClient:
    """An OllamaClient whose /api/generate returns `reply` as the response text."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"response": reply})

    return OllamaClient(transport=httpx.MockTransport(handler))


# -- extraction + validation -------------------------------------------------


def test_extracts_state_and_episode_candidates():
    reply = """[
      {"kind": "state", "key": "preferred_briefing_format", "value": "short",
       "rationale": "wants less"},
      {"kind": "episode", "episode_type": "task", "outcome": "miss",
       "context": "admin", "rationale": "blows off admin on Mondays"}
    ]"""
    cands = extract_candidates("...", client=_client(reply))
    assert len(cands) == 2
    state, episode = cands
    assert state.kind == "state" and state.payload == {
        "key": "preferred_briefing_format",
        "value": "short",
    }
    assert episode.kind == "episode"
    assert episode.payload == {"episode_type": "task", "outcome": "miss", "context": "admin"}
    assert "admin" in episode.rationale


def test_drops_disallowed_state_key_and_episode_type():
    reply = """[
      {"kind": "state", "key": "time_estimation_bias", "value": "2.0"},
      {"kind": "state", "key": "pushover_token", "value": "secret"},
      {"kind": "episode", "episode_type": "purchase", "context": "x"}
    ]"""
    assert extract_candidates("...", client=_client(reply)) == []


def test_strips_fabricated_numeric_fields_on_episode():
    reply = """[
      {"kind": "episode", "episode_type": "task", "predicted_value": 30,
       "actual_value": 90, "context": "report"}
    ]"""
    (cand,) = extract_candidates("...", client=_client(reply))
    # The model doesn't get to invent durations — only qualitative fields survive.
    assert cand.payload == {"episode_type": "task", "context": "report"}
    assert "predicted_value" not in cand.payload and "actual_value" not in cand.payload


def test_coerces_fenced_and_chatty_json():
    reply = 'Sure! Here you go:\n```json\n[{"kind":"state","key":"self_care","value":"on"}]\n```'
    (cand,) = extract_candidates("...", client=_client(reply))
    assert cand.payload == {"key": "self_care", "value": "on"}


def test_no_model_returns_no_candidates():
    """An unreachable model observes nothing rather than guessing."""
    assert extract_candidates("something", client=_client("", status=500)) == []


def test_empty_text_short_circuits():
    assert extract_candidates("   ", client=_client("[]")) == []


def test_garbage_reply_is_safe():
    assert extract_candidates("...", client=_client("no json here")) == []


# -- propose → confirm → apply ----------------------------------------------


def test_record_and_apply_state_proposal_stamps_llm_inferred(store):
    reply = '[{"kind":"state","key":"preferred_briefing_format","value":"long","rationale":"r"}]'
    ids = record_candidates(store, extract_candidates("...", client=_client(reply)))
    assert len(ids) == 1

    pending = store.list_proposals("pending")
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    # Nothing is written until accepted (the seed default is untouched).
    assert store.get_state("preferred_briefing_format") == "short"

    proposal = store.get_proposal(ids[0])
    apply_proposal(store, proposal)
    store.set_proposal_status(ids[0], "accepted")
    assert store.get_state("preferred_briefing_format") == "long"
    assert store.all_state()["preferred_briefing_format"]["source"] == "llm_inferred"


def test_apply_episode_proposal_logs_episode(store):
    reply = '[{"kind":"episode","episode_type":"task","outcome":"miss","context":"admin"}]'
    ids = record_candidates(store, extract_candidates("...", client=_client(reply)))
    apply_proposal(store, store.get_proposal(ids[0]))
    store.set_proposal_status(ids[0], "accepted")
    eps = store.episodes_by_type("task")
    assert eps and eps[0]["outcome"] == "miss" and eps[0]["context"] == "admin"


def test_status_only_moves_a_pending_proposal(store):
    pid = store.add_proposal(kind="state", payload={"key": "self_care", "value": "on"})
    assert store.set_proposal_status(pid, "accepted") is True
    # A second resolve is a no-op (keeps apply idempotent).
    assert store.set_proposal_status(pid, "rejected") is False
    assert store.get_proposal(pid)["status"] == "accepted"


def test_reject_leaves_state_untouched(store):
    pid = store.add_proposal(kind="state", payload={"key": "self_care", "value": "on"})
    store.set_proposal_status(pid, "rejected")
    assert store.get_state("self_care") != "on"
    assert store.list_proposals("pending") == []


# -- HTTP surface: POST /observe + GET/POST /proposals -----------------------

_HTTP_SECRET = "sensor-http-secret"


def _auth() -> dict[str, str]:
    return {"X-Prefrontal-Token": _HTTP_SECRET}


def _client_with(reply: str):
    """A TestClient over an in-memory store, with an Ollama that returns `reply`."""
    conn = init_db(":memory:")
    store = scoped_default(MemoryStore(conn))
    from prefrontal.webhooks.app import create_app

    app = create_app(
        store=store,
        settings=Settings(webhook_secret=_HTTP_SECRET),
        ollama=_client(reply),
    )
    return TestClient(app), store


def test_observe_records_pending_proposals():
    reply = """[
      {"kind": "state", "key": "self_care", "value": "on", "rationale": "wants reminders"},
      {"kind": "episode", "episode_type": "task", "outcome": "miss", "context": "admin"}
    ]"""
    client, store = _client_with(reply)
    with client:
        r = client.post("/observe", json={"text": "turn on self care; I blow off admin"},
                        headers=_auth())
        assert r.status_code == 201
        body = r.json()
        assert body["count"] == 2
        assert {p["kind"] for p in body["proposals"]} == {"state", "episode"}
        # Nothing applied yet — they're pending until accepted.
        assert store.get_state("self_care") != "on"
        assert store.list_proposals("pending")


def test_observe_requires_auth():
    client, _ = _client_with("[]")
    with client:
        assert client.post("/observe", json={"text": "x"}).status_code == 401


def test_observe_empty_when_model_yields_nothing():
    client, store = _client_with("[]")
    with client:
        r = client.post("/observe", json={"text": "nothing here"}, headers=_auth())
        assert r.status_code == 201 and r.json() == {"count": 0, "proposals": []}


def test_proposals_list_accept_applies_and_reject_does_not():
    client, store = _client_with("[]")
    with client:
        sid = store.add_proposal(kind="state", payload={"key": "self_care", "value": "on"})
        eid = store.add_proposal(kind="episode",
                                 payload={"episode_type": "task", "outcome": "miss"})
        listed = client.get("/proposals", headers=_auth()).json()["proposals"]
        assert {p["id"] for p in listed} == {sid, eid}

        # Accept the state proposal → applied with source=llm_inferred.
        acc = client.post(f"/proposals/{sid}/accept", headers=_auth())
        assert acc.status_code == 200 and acc.json()["status"] == "accepted"
        assert store.get_state("self_care") == "on"

        # Reject the episode proposal → resolved, nothing logged.
        rej = client.post(f"/proposals/{eid}/reject", headers=_auth())
        assert rej.status_code == 200 and rej.json()["status"] == "rejected"
        assert store.episodes_by_type("task") == []

        # Both resolved → the pending queue is empty; a re-accept 404s.
        assert client.get("/proposals", headers=_auth()).json()["proposals"] == []
        assert client.post(f"/proposals/{sid}/accept", headers=_auth()).status_code == 404


def test_proposals_unknown_action_404s():
    client, store = _client_with("[]")
    with client:
        pid = store.add_proposal(kind="state", payload={"key": "self_care", "value": "on"})
        assert client.post(f"/proposals/{pid}/frobnicate", headers=_auth()).status_code == 404


def test_review_page_served_without_auth():
    client, _ = _client_with("[]")
    with client:
        resp = client.get("/review")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        # A self-contained shell: carries no data, drives the JSON API client-side.
        assert "X-Prefrontal-Token" in resp.text
        assert "/observe" in resp.text and "/proposals" in resp.text


# -- conversation / transcript source ----------------------------------------


def test_render_transcript_labels_turns_and_skips_blanks():
    turns = [
        {"speaker": "me", "text": "I always bail on admin"},
        {"role": "coach", "content": "when?"},  # role/content fallbacks
        {"speaker": "me", "text": "   "},  # blank text → dropped
        {"text": "no speaker"},  # missing speaker → '?'
    ]
    assert render_transcript(turns) == "me: I always bail on admin\ncoach: when?\n?: no speaker"


def _capturing_client(reply: str):
    """An OllamaClient that records the last prompt/system it was asked to generate."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen["prompt"] = body.get("prompt", "")
        seen["system"] = body.get("system", "")
        return httpx.Response(200, json={"response": reply})

    return OllamaClient(transport=httpx.MockTransport(handler)), seen


def test_transcript_extraction_reads_whole_conversation_and_enforces_allowlist():
    reply = """[
      {"kind": "state", "key": "self_care", "value": "on", "rationale": "asked for meal nudges"},
      {"kind": "state", "key": "pushover_token", "value": "leaked"},
      {"kind": "episode", "episode_type": "task", "outcome": "miss", "context": "admin"}
    ]"""
    client, seen = _capturing_client(reply)
    turns = [
        {"speaker": "me", "text": "remind me to eat; I forget lunch"},
        {"speaker": "coach", "text": "and admin tasks?"},
        {"speaker": "me", "text": "I always blow those off"},
    ]
    cands = extract_candidates_from_transcript(turns, client=client)
    # Same safety gate as a note: disallowed pushover_token is dropped.
    assert {c.payload.get("key") for c in cands if c.kind == "state"} == {"self_care"}
    assert any(c.kind == "episode" for c in cands)
    # The model saw the rendered conversation, framed to attribute to the user.
    assert "I always blow those off" in seen["prompt"]
    assert "CONVERSATION:" in seen["prompt"] and "ABOUT THE USER" in seen["prompt"]


def test_transcript_extraction_empty_when_no_usable_turns():
    client, _ = _capturing_client("[]")
    blank = [{"speaker": "me", "text": "  "}]
    assert extract_candidates_from_transcript(blank, client=client) == []


def test_observe_accepts_a_transcript():
    reply = '[{"kind":"state","key":"encouragement","value":"on","rationale":"wants pep talks"}]'
    client, store = _client_with(reply)
    with client:
        r = client.post(
            "/observe",
            json={
                "transcript": [
                    {"speaker": "me", "text": "rough week, be gentle with me"},
                    {"speaker": "coach", "text": "want encouragement on?"},
                    {"speaker": "me", "text": "yes please"},
                ]
            },
            headers=_auth(),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["count"] == 1
        assert body["proposals"][0]["kind"] == "state"
        # Still pending until accepted.
        assert store.get_state("encouragement") != "on"
        assert store.list_proposals("pending")


def test_observe_422s_when_neither_text_nor_transcript():
    client, _ = _client_with("[]")
    with client:
        assert client.post("/observe", json={}, headers=_auth()).status_code == 422
        assert client.post(
            "/observe", json={"text": "   ", "transcript": []}, headers=_auth()
        ).status_code == 422
