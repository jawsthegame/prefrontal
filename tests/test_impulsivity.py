"""Tests for the Impulsivity module — capture-and-defer.

Covers the pure title helpers (heuristic + LLM-with-fallback) and the
``POST /webhooks/impulse/capture`` endpoint that parks an impulse as a
``source='impulse'`` todo and reads back a speakable confirmation.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.impulsivity import (
    heuristic_capture_title,
    infer_capture_title,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "impulse-secret"


def _offline_ollama() -> OllamaClient:
    """An OllamaClient whose every call fails — forces the heuristic path."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_replying(text: str) -> OllamaClient:
    """An OllamaClient whose generate() returns `text` (for the LLM path)."""
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    """A TestClient with auth on and Ollama offline (deterministic heuristic titling)."""
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


# -- pure title helpers ------------------------------------------------------


def test_heuristic_title_truncates_and_capitalizes():
    long = "reorganize the entire font folder and rename every single weight"
    title = heuristic_capture_title(long)
    assert title.startswith("Reorganize the entire font folder")
    assert title.endswith("…")  # truncated past the word cap
    assert len(title.split()) <= 9  # 8 words + the ellipsis token


def test_heuristic_title_blank_is_safe():
    assert heuristic_capture_title("   ") == "Captured impulse"


def test_infer_title_uses_llm_when_available():
    title = infer_capture_title(
        "ooh the thing with the fonts", client=_ollama_replying("Reorganize font folder")
    )
    assert title == "Reorganize font folder"


def test_infer_title_strips_quotes_and_overlong_llm_reply():
    """A chatty model reply is cleaned: quotes stripped, word count capped."""
    title = infer_capture_title(
        "x", client=_ollama_replying('"call the dentist and also book the car and the vet today"')
    )
    assert '"' not in title
    assert len(title.split()) <= 8


def test_infer_title_falls_back_when_model_down():
    title = infer_capture_title("call the plumber", client=_offline_ollama())
    assert title == "Call the plumber"  # heuristic fallback


def test_infer_title_blank_is_none():
    assert infer_capture_title("   ", client=None) is None


# -- HTTP: capture-and-defer -------------------------------------------------


def test_capture_creates_impulse_todo(client, store):
    """A captured impulse becomes an open todo flagged source='impulse'."""
    resp = client.post(
        "/webhooks/impulse/capture",
        json={"impulse_text": "reorganize the reference folder"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["raw"] == "reorganize the reference folder"
    # It lands in the normal open-loop list, marked as an impulse.
    todo = store.get_todo(body["todo_id"])
    assert todo["status"] == "open"
    assert todo["source"] == "impulse"
    assert todo["notes"] == "reorganize the reference folder"  # raw text preserved


def test_capture_confirmation_is_speakable(client):
    """The read-back names the parked item so a Shortcut shows it verbatim."""
    body = client.post(
        "/webhooks/impulse/capture",
        json={"impulse_text": "email Dana about the venue"},
        headers=_auth(),
    ).json()
    conf = body["confirmation"]
    assert "Parked" in conf
    assert body["title"] in conf
    assert "safe" in conf


def test_capture_rejects_blank(client):
    resp = client.post(
        "/webhooks/impulse/capture", json={"impulse_text": "   "}, headers=_auth()
    )
    assert resp.status_code == 422


def test_capture_requires_auth(client):
    assert client.post(
        "/webhooks/impulse/capture", json={"impulse_text": "x"}
    ).status_code == 401
