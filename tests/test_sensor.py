"""Tests for the LLM-as-sensor (learning §2).

Covers the extractor's grounded JSON parsing + allowlist validation, the honest
no-model fallback, and the propose→confirm→apply loop (candidates land pending
and only reach the store — stamped source='llm_inferred' — on accept).
"""

from __future__ import annotations

import httpx
import pytest

from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.store import MemoryStore
from prefrontal.sensor import (
    apply_proposal,
    extract_candidates,
    record_candidates,
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
