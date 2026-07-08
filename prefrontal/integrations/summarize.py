"""One place to turn a deterministic digest into optional LLM prose.

Several domain services (:func:`prefrontal.panic.summarize_panic`,
:func:`prefrontal.briefing.summarize_briefing`,
:func:`prefrontal.encouragement.summarize_encouragement`) share the exact same
shape: build a complete deterministic digest, render it to text, then *optionally*
rewrite that text as warmer/clearer prose via the model — falling back to the
rendered text when the model is unavailable, fails, or returns nothing. The
local-first contract is that the deterministic text is always a valid answer, so
a model hiccup degrades quietly rather than erroring.

That block was previously copy-pasted per service, which let two subtle bugs
drift in: some copies caught only
:class:`~prefrontal.integrations.ollama.OllamaError` (so a failure of the *other*
provider would not fall back), and some defaulted straight to a local
:class:`~prefrontal.integrations.ollama.OllamaClient`, bypassing the per-agent
:class:`~prefrontal.integrations.provider.ProviderResolver`. :func:`summarize_or_fallback`
is the single implementation: it resolves the client through the resolver (so the
``ANTHROPIC_AGENTS`` policy has one home) and catches the shared
:class:`~prefrontal.integrations.base.ProviderError` base (so *either* backend
failing degrades to the heuristic).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prefrontal.integrations.base import ProviderError
from prefrontal.integrations.provider import ProviderResolver

if TYPE_CHECKING:
    from prefrontal.integrations import Generator


@dataclass(frozen=True)
class Summary:
    """The outcome of an optional LLM prose rewrite over a deterministic digest.

    Attributes:
        text: The prose (``source="llm"``) or the deterministic digest it fell
            back to (``source="heuristic"``).
        source: ``"llm"`` when the model produced the text, ``"heuristic"`` when
            it fell back to the rendered digest.
        model: The model name when ``source="llm"``, else ``None``.
    """

    text: str
    source: str  # "llm" | "heuristic"
    model: str | None = None


def summarize_or_fallback(
    rendered: str,
    *,
    system: str,
    agent: str,
    client: Generator | None = None,
    fallback: bool = True,
) -> Summary:
    """Rewrite ``rendered`` as prose via the model, falling back to it on failure.

    Args:
        rendered: The complete deterministic digest. This is the fallback text —
            it must already be a valid, self-sufficient answer.
        system: The system prompt steering the rewrite.
        agent: The agent name used to resolve the provider (see
            :class:`~prefrontal.integrations.provider.ProviderResolver`). Only the
            selectable agents in
            :data:`~prefrontal.integrations.provider.KNOWN_AGENTS` (or the ``all``
            sentinel) ever route to Anthropic; anything else stays local — so a
            non-selectable summarizer behaves exactly as before unless the operator
            opts every agent in.
        client: A pre-resolved client to use verbatim (tests inject an offline
            one; a caller that already resolved can pass it to avoid re-resolving).
            When ``None``, the client is resolved via ``ProviderResolver`` for
            ``agent``.
        fallback: When ``True`` (default), any provider failure or empty response
            returns the rendered digest as a ``"heuristic"`` result. When
            ``False``, the failure is raised.

    Returns:
        A :class:`Summary` — prose when the model succeeds, else the rendered
        digest.

    Raises:
        prefrontal.integrations.base.ProviderError: If the model fails (or returns
            nothing) and ``fallback`` is ``False``.
    """
    resolved = client or ProviderResolver.from_settings().client(agent)
    try:
        prose = resolved.generate(rendered, system=system).strip()
    except ProviderError:
        if not fallback:
            raise
        return Summary(text=rendered, source="heuristic")
    if not prose:
        if not fallback:
            raise ProviderError(f"The {agent} model returned an empty summary.")
        return Summary(text=rendered, source="heuristic")
    return Summary(text=prose, source="llm", model=getattr(resolved, "model", None))
