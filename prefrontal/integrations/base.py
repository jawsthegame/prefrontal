"""Shared base types for inference-provider clients.

Both the local :class:`~prefrontal.integrations.ollama.OllamaClient` and the
optional cloud :class:`~prefrontal.integrations.anthropic.AnthropicClient` raise
a subclass of :class:`ProviderError` on failure. A provider-agnostic call site —
one that may be handed *either* backend by
:class:`~prefrontal.integrations.provider.ProviderResolver` — can therefore catch
the single base type and fall back to its heuristic/local path without caring
which provider it was actually talking to.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base for inference-provider failures (missing SDK, auth, or transport)."""
