"""Tests for the LLM-backed summarizer and the Ollama client.

The Ollama client is exercised with an ``httpx.MockTransport`` so no real server
is needed. The summarizer is tested with a fake/stub client to cover the LLM
path, the graceful fallback to the structured profile, and the no-fallback error
path.
"""

from __future__ import annotations

import httpx
import pytest

from prefrontal.integrations.ollama import OllamaClient, OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import summarize_profile


@pytest.fixture()
def store():
    """An in-memory store with the seed coaching state."""
    with MemoryStore.open(":memory:") as s:
        yield s


# -- Ollama client -----------------------------------------------------------


def _client_with(handler) -> OllamaClient:
    """An OllamaClient whose HTTP calls are served by `handler`."""
    return OllamaClient(transport=httpx.MockTransport(handler))


def test_generate_returns_response_text():
    """generate() posts to /api/generate and returns the 'response' field."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"response": "  do the thing  "})

    client = _client_with(handler)
    out = client.generate("the structured profile", system="be concise")
    assert out == "do the thing"  # stripped
    assert captured["url"].endswith("/api/generate")
    assert "be concise" in captured["body"]


def test_generate_raises_on_http_error():
    """A non-2xx response becomes an OllamaError."""
    client = _client_with(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(OllamaError):
        client.generate("x")


def test_generate_raises_on_missing_field():
    """A 200 without a 'response' string is an OllamaError."""
    client = _client_with(lambda req: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(OllamaError):
        client.generate("x")


def test_available_true_and_false():
    """available() reflects whether /api/tags succeeds, never raising."""
    ok = _client_with(lambda req: httpx.Response(200, json={"models": []}))
    assert ok.available() is True

    def boom(request):
        raise httpx.ConnectError("refused")

    assert _client_with(boom).available() is False


# -- summarizer --------------------------------------------------------------


class _FakeClient:
    """Stub Ollama client capturing the prompt and returning a canned reply."""

    def __init__(self, reply="", model="fake-model", error=False):
        self.reply = reply
        self.model = model
        self.error = error
        self.prompt = None
        self.system = None

    def generate(self, prompt, *, system=None):
        if self.error:
            raise OllamaError("model down")
        self.prompt = prompt
        self.system = system
        return self.reply


def test_summarize_uses_llm_when_available(store):
    """A working model yields source='llm' and its text; the structured profile
    is passed to the model as input."""
    fake = _FakeClient(reply="Lead with departures: pad estimates 1.4x.")
    result = summarize_profile(store, client=fake)
    assert result.source == "llm"
    assert result.model == "fake-model"
    assert result.text == "Lead with departures: pad estimates 1.4x."
    # The model received the structured profile as grounding.
    assert "Behavioral profile" in fake.prompt
    assert result.structured.startswith("# Behavioral profile")


def test_summarize_falls_back_on_model_error(store):
    """A model error falls back to the structured profile by default."""
    result = summarize_profile(store, client=_FakeClient(error=True))
    assert result.source == "heuristic"
    assert result.model is None
    assert "Behavioral profile" in result.text


def test_summarize_falls_back_on_empty_reply(store):
    """An empty model reply also falls back rather than emitting nothing."""
    result = summarize_profile(store, client=_FakeClient(reply="   "))
    assert result.source == "heuristic"
    assert result.text.strip() != ""


def test_summarize_no_fallback_raises(store):
    """With fallback disabled, a model error propagates."""
    with pytest.raises(OllamaError):
        summarize_profile(store, client=_FakeClient(error=True), fallback=False)
