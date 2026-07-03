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
from prefrontal.memory.summarizer import (
    cache_is_stale,
    cache_summary,
    load_cached_summary,
    refresh_profile_cache,
    summarize_profile,
)
from tests.conftest import scoped_default


@pytest.fixture()
def store():
    """An in-memory store with the seed coaching state."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


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


# -- narrative cache ---------------------------------------------------------


def test_load_cached_summary_none_when_empty(store):
    """Nothing is cached on a fresh store."""
    assert load_cached_summary(store) is None


def test_cache_summary_round_trip(store):
    """cache_summary persists a ProfileSummary that load_cached_summary returns."""
    summary = summarize_profile(store, client=_FakeClient(reply="pad estimates 1.4x."))
    cache_summary(store, summary)

    loaded = load_cached_summary(store)
    assert loaded.text == "pad estimates 1.4x."
    assert loaded.source == "llm"
    assert loaded.model == "fake-model"
    assert loaded.structured.startswith("# Behavioral profile")
    # generated_at is populated only on the loaded (cached) copy.
    assert summary.generated_at is None
    assert loaded.generated_at


def test_refresh_profile_cache_generates_and_persists(store):
    """refresh_profile_cache returns a fresh summary and writes it to the cache."""
    result = refresh_profile_cache(store, client=_FakeClient(reply="do the next thing."))
    assert result.source == "llm"
    assert load_cached_summary(store).text == "do the next thing."


def test_cache_is_stale_detects_changes(store):
    """The cache is fresh right after writing, stale once the facts move on."""
    refresh_profile_cache(store, client=_FakeClient(reply="prose"))
    assert cache_is_stale(store) is False

    # Change an underlying fact: the structured profile no longer matches.
    store.set_state("time_estimation_bias", "1.9", source="explicit")
    assert cache_is_stale(store) is True


def test_cache_is_stale_when_empty(store):
    """A missing cache counts as stale (nothing to trust)."""
    assert cache_is_stale(store) is True


# -- bias calibration surfacing in the profile (learning §4) -----------------


def test_profile_surfaces_bias_calibration_verdict(store):
    from prefrontal.memory.summarizer import build_profile

    store.set_state("time_estimation_bias", "1.4", source="inferred")

    store.set_state("bias_calibration_helps", "true", source="inferred")
    store.set_state("bias_calibration_improvement", "3.0", source="inferred")
    helping = build_profile(store, modules=[])
    assert "improving recent estimates" in helping and "3.0 closer" in helping

    store.set_state("bias_calibration_helps", "false", source="inferred")
    hurting = build_profile(store, modules=[])
    assert "NOT improving recent estimates" in hurting


def test_profile_surfaces_time_of_day_bias(store):
    from prefrontal.memory.summarizer import build_profile

    store.set_state("time_estimation_bias", "1.4", source="inferred")
    store.set_state("time_estimation_bias:morning", "1.8", source="inferred")
    store.set_state("time_estimation_bias:afternoon", "1.1", source="inferred")
    out = build_profile(store, modules=[])
    assert "By time of day: morning 1.8x, afternoon 1.1x." in out


def test_profile_surfaces_task_type_bias(store):
    from prefrontal.memory.patterns import TYPE_BIAS_PREFIX
    from prefrontal.memory.summarizer import build_profile

    store.set_state("time_estimation_bias", "1.4", source="inferred")
    store.set_state(f"{TYPE_BIAS_PREFIX}task", "1.9", source="inferred")
    store.set_state(f"{TYPE_BIAS_PREFIX}departure", "1.2", source="inferred")
    out = build_profile(store, modules=[])
    assert "By task type: departure 1.2x, task 1.9x." in out
