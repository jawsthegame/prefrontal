"""Optional Claude (Anthropic) inference client.

Prefrontal is local-first: reasoning runs on the host via Ollama by default. This
module is the *opt-in* cloud path — when an operator sets an Anthropic API key,
the dashboard assistant (natural-language edits to todos/commitments) can use
Claude for more reliable structured parsing, with Ollama staying as the fallback.

It deliberately mirrors :class:`~prefrontal.integrations.ollama.OllamaClient`'s
tiny surface — ``generate(prompt, *, system=None) -> str`` and ``available()`` —
so the two are interchangeable behind the ``Generator`` protocol callers already
use. Today only the dashboard assistant selects this backend (Claude when
available, else Ollama); the todo augmenter/decomposer still run on the local
model. Whichever backend answers, the reply is parsed the same way (see
:mod:`prefrontal.llm_json`).

The official ``anthropic`` SDK is an *optional* dependency (``pip install
prefrontal[anthropic]``); it is imported lazily so the base install stays
network-free. A missing SDK or unset key simply makes :meth:`available` return
``False`` — callers fall back to the local model rather than erroring.
"""

from __future__ import annotations

from typing import Any

from prefrontal.config import Settings, get_settings
from prefrontal.integrations.base import ProviderError

#: Default Claude model — the most capable Opus-tier model. Overridable via
#: ``ANTHROPIC_MODEL`` for cost/latency tuning.
DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicError(ProviderError):
    """Raised when an Anthropic request fails (missing SDK, auth, or transport)."""


class AnthropicClient:
    """Synchronous Claude client with the same shape as :class:`OllamaClient`.

    Args:
        api_key: Anthropic API key. Empty disables the client (:meth:`available`
            returns ``False``) so callers fall back to the local model.
        model: Model id to generate with (defaults to :data:`DEFAULT_MODEL`).
        timeout: Per-request timeout in seconds.
        max_tokens: Output-token cap. Assistant action lists are small, so the
            default is modest and keeps the request well under the HTTP timeout.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        max_tokens: int = 1024,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AnthropicClient:
        """Build a client from :class:`~prefrontal.config.Settings`."""
        resolved = settings or get_settings()
        return cls(api_key=resolved.anthropic_api_key, model=resolved.anthropic_model)

    def available(self) -> bool:
        """Return ``True`` if a key is set and the ``anthropic`` SDK is importable.

        Never raises and never hits the network — it's cheap enough to call on
        every request to decide whether to prefer Claude over the local model.
        """
        if not self.api_key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Generate a single completion, returning the concatenated text blocks.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.

        Returns:
            The model's response text (stripped). A safety refusal yields an
            empty string rather than raising, so callers treat it as "no usable
            reply" and fall through to their heuristic path.

        Raises:
            AnthropicError: If the SDK is missing, the key is unset, or the
                request fails — so callers can fall back to the local model.
        """
        if not self.api_key:
            raise AnthropicError("No Anthropic API key configured.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise AnthropicError(
                "The 'anthropic' package is not installed "
                "(pip install prefrontal[anthropic])."
            ) from exc

        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        try:
            resp = client.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            raise AnthropicError(f"Anthropic request failed: {exc}") from exc

        # A safety refusal (or any non-text stop) leaves no usable content — return
        # empty so the caller falls back rather than parsing a refusal string.
        if getattr(resp, "stop_reason", None) == "refusal":
            return ""
        parts = [
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts).strip()
