"""Tests for the shared summarize-or-fallback helper.

Covers :func:`prefrontal.integrations.summarize.summarize_or_fallback` — the one
place the prose-rewrite-with-heuristic-fallback lives now that panic/briefing/
encouragement delegate to it. The key guarantee is that *any* provider failure
(not just OllamaError) degrades to the deterministic text.
"""

from __future__ import annotations

import pytest

from prefrontal.integrations.anthropic import AnthropicError
from prefrontal.integrations.base import ProviderError
from prefrontal.integrations.ollama import OllamaError
from prefrontal.integrations.summarize import Summary, summarize_or_fallback


class _FakeClient:
    def __init__(self, reply: str = "", error: Exception | None = None, model: str = "fake"):
        self.reply, self.error, self.model = reply, error, model

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if self.error is not None:
            raise self.error
        return self.reply


def test_returns_llm_prose_on_success():
    result = summarize_or_fallback(
        "digest", system="s", agent="briefing", client=_FakeClient(reply="  warm prose  ")
    )
    assert result == Summary(text="warm prose", source="llm", model="fake")


def test_falls_back_on_ollama_error():
    result = summarize_or_fallback(
        "digest", system="s", agent="panic", client=_FakeClient(error=OllamaError("down"))
    )
    assert result == Summary(text="digest", source="heuristic", model=None)


def test_falls_back_on_non_ollama_provider_error():
    # The regression this helper exists to prevent: an AnthropicError is a
    # ProviderError but not an OllamaError, so a narrow catch would let it escape.
    result = summarize_or_fallback(
        "digest", system="s", agent="briefing", client=_FakeClient(error=AnthropicError("429"))
    )
    assert result.source == "heuristic" and result.text == "digest"


def test_falls_back_on_empty_response():
    result = summarize_or_fallback(
        "digest", system="s", agent="panic", client=_FakeClient(reply="   ")
    )
    assert result.source == "heuristic" and result.text == "digest"


def test_reraises_provider_error_when_fallback_disabled():
    with pytest.raises(AnthropicError):
        summarize_or_fallback(
            "digest",
            system="s",
            agent="briefing",
            client=_FakeClient(error=AnthropicError("boom")),
            fallback=False,
        )


def test_empty_response_raises_when_fallback_disabled():
    with pytest.raises(ProviderError):
        summarize_or_fallback(
            "digest", system="s", agent="panic", client=_FakeClient(reply=""), fallback=False
        )
