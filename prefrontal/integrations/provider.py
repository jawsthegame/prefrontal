"""Per-agent inference-provider selection (local-first, opt-in cloud).

Prefrontal runs inference on the local Ollama model by default. An operator can
opt individual *agents* into the cloud Anthropic provider via the
``ANTHROPIC_AGENTS`` setting (see
:attr:`~prefrontal.config.Settings.anthropic_agents`) — e.g. Claude for the
profile summary while everything else stays local. This is the per-agent
selectability the README promises: "heavier reasoning tasks can optionally use
the Anthropic API — configurable per agent."

:class:`ProviderResolver` turns that config into a concrete client per agent. It
returns the Anthropic client for an opted-in agent when a key is configured and
the SDK is importable (:meth:`~prefrontal.integrations.anthropic.AnthropicClient.available`),
and the local Ollama client otherwise. Selection is by *availability only* — no
network call — so it's cheap to resolve on every request.

If the chosen Anthropic call then fails at request time, the consuming agent
still degrades gracefully, because every selectable call site catches
:class:`~prefrontal.integrations.base.ProviderError` (the shared base of both
backends' errors) and falls back to its heuristic/local path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prefrontal.config import Settings, get_settings
from prefrontal.integrations.anthropic import AnthropicClient
from prefrontal.integrations.ollama import OllamaClient

if TYPE_CHECKING:
    from prefrontal.integrations import Generator

#: Selectable agents — the reasoning-heavy, quality-sensitive call sites where a
#: better model earns its cost. The snappy in-loop inferers (window/title/kind
#: classification, decomposition) stay local by design for latency, so they are
#: intentionally *not* here. Used for documentation and config validation.
ASSISTANT = "assistant"
SUMMARIZER = "summarizer"
BRIEFING = "briefing"
SENSOR = "sensor"
TRIAGE = "triage"
KNOWN_AGENTS = frozenset({ASSISTANT, SUMMARIZER, BRIEFING, SENSOR, TRIAGE})

#: Sentinel value in ``anthropic_agents`` meaning "every agent prefers Anthropic".
ALL_AGENTS = "all"


@dataclass(frozen=True)
class ProviderResolver:
    """Resolve the inference backend for a named agent (Claude vs local Ollama).

    Attributes:
        ollama: The default local client, used as the fallback for any agent not
            opted into Anthropic (or when Anthropic is unavailable).
        anthropic: The optional cloud client. When its key/SDK are absent,
            :meth:`~prefrontal.integrations.anthropic.AnthropicClient.available`
            is ``False`` and every agent stays local.
        anthropic_agents: The agent names (or the :data:`ALL_AGENTS` sentinel)
            that prefer Anthropic, from
            :attr:`~prefrontal.config.Settings.anthropic_agents`.
    """

    ollama: OllamaClient
    anthropic: AnthropicClient
    anthropic_agents: frozenset[str]

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        ollama: OllamaClient | None = None,
        anthropic: AnthropicClient | None = None,
    ) -> ProviderResolver:
        """Build a resolver from settings, constructing default clients as needed.

        Args:
            settings: Resolved settings (defaults to :func:`get_settings`).
            ollama: Pre-built local client (tests inject an offline one).
            anthropic: Pre-built cloud client (defaults to one from settings).
        """
        resolved = settings or get_settings()
        return cls(
            ollama=ollama or OllamaClient.from_settings(resolved),
            anthropic=anthropic or AnthropicClient.from_settings(resolved),
            anthropic_agents=frozenset(resolved.anthropic_agents),
        )

    def prefers_anthropic(self, agent: str) -> bool:
        """Whether ``agent`` is configured to prefer Anthropic (ignores availability)."""
        return ALL_AGENTS in self.anthropic_agents or agent in self.anthropic_agents

    def select(
        self, agent: str, *, fallback: Generator | None = None
    ) -> tuple[Generator, str]:
        """Pick ``(client, provider_name)`` for ``agent``.

        Returns the Anthropic client (``"anthropic"``) when the agent prefers it
        *and* it's available; otherwise the local fallback (``"ollama"``).

        Args:
            agent: A selectable agent name (see :data:`KNOWN_AGENTS`).
            fallback: The local client to use when Anthropic isn't selected.
                Defaults to :attr:`ollama` — pass the longer-timeout summarizer
                client for the heavier profile/briefing generations.
        """
        local = fallback if fallback is not None else self.ollama
        if self.prefers_anthropic(agent) and self.anthropic.available():
            return self.anthropic, "anthropic"
        return local, "ollama"

    def client(self, agent: str, *, fallback: Generator | None = None) -> Generator:
        """Return just the selected client for ``agent`` (see :meth:`select`)."""
        return self.select(agent, fallback=fallback)[0]

    def unknown_agents(self) -> frozenset[str]:
        """Configured agent names that don't match a real agent (operator typos)."""
        return frozenset(self.anthropic_agents) - KNOWN_AGENTS - {ALL_AGENTS}
