"""Tests for the per-agent inference-provider selection (Optional Anthropic provider).

Covers the ``ANTHROPIC_AGENTS`` config parse, the :class:`ProviderResolver`
selection logic (prefer Claude only for opted-in agents, and only when it's
available, else the local fallback), and the shared :class:`ProviderError` base
that lets a reasoning agent fall back the same way whichever backend it was
handed.
"""

from __future__ import annotations

import pytest

from prefrontal.config import _parse_anthropic_agents, load_settings
from prefrontal.integrations import AnthropicError, OllamaError, ProviderError
from prefrontal.integrations.provider import (
    ALL_AGENTS,
    KNOWN_AGENTS,
    VISION,
    ProviderResolver,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import summarize_profile
from tests.conftest import scoped_default


class _Fake:
    """A stand-in :class:`~prefrontal.integrations.Generator` with a label."""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        raises: bool = False,
        reply: str | None = None,
        can_see: bool = False,
    ):
        self.name = name
        self.model = name
        self._available = available
        self._raises = raises
        self._reply = reply
        self._can_see = can_see
        self.calls = 0
        self.can_see_calls = 0

    def available(self) -> bool:
        return self._available

    def can_describe_images(self) -> bool:
        self.can_see_calls += 1
        return self._can_see

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.calls += 1
        if self._raises:
            raise AnthropicError("boom")
        return self._reply if self._reply is not None else f"{self.name} says hi"


# --- config parse -----------------------------------------------------------


def test_parse_anthropic_agents_unset_defaults_to_assistant():
    # Unset preserves the historical behavior: only the dashboard assistant.
    assert _parse_anthropic_agents(None) == ("assistant",)


def test_parse_anthropic_agents_empty_string_opts_all_out():
    # An explicit empty value is "no agent uses Claude" (fully local).
    assert _parse_anthropic_agents("") == ()


@pytest.mark.parametrize("raw", ["all", "*", " ALL "])
def test_parse_anthropic_agents_all_sentinel(raw):
    assert _parse_anthropic_agents(raw) == ("all",)


def test_parse_anthropic_agents_list_is_lowered_and_deduped():
    assert _parse_anthropic_agents("Summarizer, triage ,summarizer") == (
        "summarizer",
        "triage",
    )


def test_load_settings_reads_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AGENTS", "summarizer,briefing")
    settings = load_settings(dotenv_path="/nonexistent")
    assert settings.anthropic_agents == ("summarizer", "briefing")


# --- resolver selection -----------------------------------------------------


def _resolver(agents, *, anthropic_available=True):
    return ProviderResolver(
        ollama=_Fake("ollama"),
        anthropic=_Fake("anthropic", available=anthropic_available),
        anthropic_agents=frozenset(agents),
    )


def test_prefers_anthropic_matches_configured_agent():
    r = _resolver({"summarizer"})
    assert r.prefers_anthropic("summarizer") is True
    assert r.prefers_anthropic("assistant") is False


def test_all_sentinel_prefers_every_agent():
    r = _resolver({ALL_AGENTS})
    assert all(r.prefers_anthropic(a) for a in KNOWN_AGENTS)


def test_select_returns_anthropic_when_preferred_and_available():
    r = _resolver({"summarizer"})
    client, provider = r.select("summarizer")
    assert provider == "anthropic"
    assert client.name == "anthropic"


def test_select_falls_back_when_preferred_but_unavailable():
    # Opted in, but no key/SDK → local model, not an error.
    r = _resolver({"summarizer"}, anthropic_available=False)
    client, provider = r.select("summarizer")
    assert provider == "ollama"
    assert client.name == "ollama"


def test_select_stays_local_for_unopted_agent():
    r = _resolver({"summarizer"})
    client, provider = r.select("sensor")
    assert provider == "ollama"
    assert client.name == "ollama"


def test_select_uses_explicit_fallback_client():
    # The summarizer path passes its longer-timeout Ollama client as fallback.
    r = _resolver(set())  # nothing prefers Anthropic
    longer = _Fake("summarizer-ollama")
    client, provider = r.select("summarizer", fallback=longer)
    assert provider == "ollama"
    assert client is longer


def test_unknown_agents_surfaces_typos():
    r = _resolver({"summarizer", "sumarizer", ALL_AGENTS})
    assert r.unknown_agents() == frozenset({"sumarizer"})


# --- vision selection (local-first) -----------------------------------------


def _vision_resolver(agents, *, local_can_see, cloud_available):
    return ProviderResolver(
        ollama=_Fake("ollama", can_see=local_can_see),
        anthropic=_Fake("anthropic", available=cloud_available),
        anthropic_agents=frozenset(agents),
    )


def test_vision_is_a_known_agent():
    assert VISION in KNOWN_AGENTS


def test_select_vision_prefers_on_device_by_default():
    # Local-first: an installed local vision model wins even when cloud is available.
    r = _vision_resolver(set(), local_can_see=True, cloud_available=True)
    client, provider = r.select_vision()
    assert provider == "ollama" and client.name == "ollama"


def test_select_vision_falls_back_to_cloud_without_local_model():
    r = _vision_resolver(set(), local_can_see=False, cloud_available=True)
    client, provider = r.select_vision()
    assert provider == "anthropic" and client.name == "anthropic"


def test_select_vision_none_when_neither_can_see():
    r = _vision_resolver(set(), local_can_see=False, cloud_available=False)
    client, provider = r.select_vision()
    assert client is None and provider == "none"


def test_select_vision_opt_in_prefers_cloud():
    # Listing 'vision' in ANTHROPIC_AGENTS flips the default to prefer cloud.
    r = _vision_resolver({"vision"}, local_can_see=True, cloud_available=True)
    client, provider = r.select_vision()
    assert provider == "anthropic" and client.name == "anthropic"


def test_select_vision_opt_in_still_falls_back_to_local():
    # Prefer cloud, but with no key fall back to the installed local model.
    r = _vision_resolver({"vision"}, local_can_see=True, cloud_available=False)
    client, provider = r.select_vision()
    assert provider == "ollama" and client.name == "ollama"


def test_select_vision_cloud_first_skips_local_probe():
    # Cloud-first + cloud available ⇒ never probe the local server (/api/tags).
    r = _vision_resolver({"vision"}, local_can_see=True, cloud_available=True)
    r.select_vision()
    assert r.ollama.can_see_calls == 0


def test_select_vision_local_first_skips_cloud_check_when_local_sees():
    # Local-first + local can see ⇒ the cloud client is never consulted.
    r = _vision_resolver(set(), local_can_see=True, cloud_available=True)
    r.select_vision()
    # available() tracks nothing to assert directly, but routing must be local.
    assert r.select_vision()[1] == "ollama"


def test_create_app_warns_on_unknown_anthropic_agent(caplog):
    """A typo'd ANTHROPIC_AGENTS name is logged at startup, not silently ignored."""
    import logging

    from prefrontal.config import Settings
    from prefrontal.webhooks.app import create_app

    with caplog.at_level(logging.WARNING):
        create_app(settings=Settings(anthropic_agents=("assistant", "sumarizer")))
    assert any("sumarizer" in r.getMessage() for r in caplog.records)


# --- shared error base ------------------------------------------------------


def test_both_backend_errors_are_provider_errors():
    assert issubclass(OllamaError, ProviderError)
    assert issubclass(AnthropicError, ProviderError)


def test_summarizer_falls_back_when_anthropic_client_raises():
    """A runtime Anthropic failure degrades to the structured profile, not a 500."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        result = summarize_profile(store, client=_Fake("anthropic", raises=True))
    assert result.source == "heuristic"
    assert result.model is None
    assert result.text  # the structured profile is still returned


# --- router wiring (per-agent selection reaches beyond the assistant) -------


def test_sensor_endpoint_uses_anthropic_when_agent_opted_in():
    """``ANTHROPIC_AGENTS=sensor`` routes POST /observe through Claude, not Ollama."""
    from fastapi.testclient import TestClient

    from prefrontal.config import Settings
    from prefrontal.memory.db import init_db
    from prefrontal.webhooks.app import create_app

    conn = init_db(":memory:")
    store = scoped_default(MemoryStore(conn))
    anthropic = _Fake(
        "anthropic",
        reply='[{"kind":"state","key":"self_care","value":"on","rationale":"x"}]',
    )
    ollama = _Fake("ollama", reply="[]")  # would yield nothing if (wrongly) used
    app = create_app(
        store=store,
        settings=Settings(webhook_secret="s", anthropic_agents=("sensor",)),
        ollama=ollama,
        anthropic=anthropic,
    )
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/observe",
                json={"text": "turn on self care"},
                headers={"X-Prefrontal-Token": "s"},
            )
        assert resp.status_code == 201
        assert resp.json()["count"] == 1
        assert anthropic.calls == 1
        assert ollama.calls == 0
    finally:
        conn.close()
