"""Optional Claude (Anthropic) inference client.

Prefrontal is local-first: reasoning runs on the host via Ollama by default. This
module is the *opt-in* cloud path — when an operator sets an Anthropic API key,
the dashboard assistant (natural-language edits to todos/commitments) can use
Claude for more reliable structured parsing, with Ollama staying as the fallback.

For text it deliberately mirrors
:class:`~prefrontal.integrations.ollama.OllamaClient`'s tiny surface —
``generate(prompt, *, system=None) -> str`` and ``available()`` — so the two are
interchangeable behind the ``Generator`` protocol callers already use. Today only
the dashboard assistant selects this backend (Claude when available, else Ollama);
the todo augmenter/decomposer still run on the local model. Whichever backend
answers, the reply is parsed the same way (see :mod:`prefrontal.llm_json`).

It also carries one capability the local model doesn't yet have —
:meth:`~AnthropicClient.describe_image`, a multimodal read of a photo. That's
Anthropic-only and off the shared protocol, so its callers gate on
:meth:`~AnthropicClient.available` and degrade rather than falling back to a
text-only model (routing vision through an on-device multimodal model is still
ahead on the roadmap).

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

#: Image media types the Anthropic vision API accepts. :meth:`describe_image`
#: rejects anything else up-front rather than paying for a request the API will
#: refuse — the caller sniffs the real type and passes it in.
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


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

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        num_ctx: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Generate a single completion, returning the concatenated text blocks.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.
            num_ctx: Ignored — an Ollama-specific context-window hint, accepted only
                to satisfy the shared :class:`~prefrontal.integrations.Generator`
                protocol (hosted models size their own context).
            timeout: Ignored here for the same reason (transport timeout is fixed
                at construction).

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

        return self._text(resp)

    def describe_image(
        self,
        image_base64: str,
        *,
        prompt: str,
        media_type: str = "image/jpeg",
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Transcribe/describe an image with the multimodal model, returning text.

        The cloud counterpart to on-device vision (roadmap: routing this through
        an on-device multimodal model is still ahead). It sends one image plus a
        text ``prompt`` and returns the model's reading of it — e.g. the text on a
        photographed whiteboard, receipt, or handwritten list — which a caller
        then feeds into an existing text pipeline (see
        :func:`prefrontal.vision.plan_vision`).

        Deliberately Anthropic-only: the local :class:`OllamaClient` exposes no
        ``describe_image`` (it's off the shared :class:`Generator` protocol), so
        callers gate on :meth:`available` and degrade rather than falling back to a
        text-only model that can't see the image.

        Args:
            image_base64: The image bytes, base64-encoded (no ``data:`` prefix) —
                the same shape the HTTP body and the SDK's image block carry, so no
                re-encoding happens in this method.
            prompt: What to do with the image, e.g. "Transcribe every item on this
                whiteboard as a plain list."
            media_type: The image's MIME type; must be in
                :data:`SUPPORTED_IMAGE_MEDIA_TYPES`.
            system: Optional system prompt.
            max_tokens: Output-token cap for this call. Defaults to the client's
                :attr:`max_tokens`; pass a larger value for image-heavy
                transcriptions that would otherwise truncate.

        Returns:
            The model's response text (stripped). A safety refusal yields an empty
            string, exactly as :meth:`generate` does, so callers treat it as "no
            usable reply" and fall through to their heuristic path.

        Raises:
            AnthropicError: If the SDK is missing, the key is unset, the request
                fails, or ``media_type`` is unsupported — so callers can fall back.
        """
        if not self.api_key:
            raise AnthropicError("No Anthropic API key configured.")
        if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise AnthropicError(
                f"Unsupported image media type {media_type!r}; expected one of "
                f"{', '.join(sorted(SUPPORTED_IMAGE_MEDIA_TYPES))}."
            )
        if not image_base64:
            raise AnthropicError("No image data provided.")
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
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        if system:
            kwargs["system"] = system
        try:
            resp = client.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            raise AnthropicError(f"Anthropic vision request failed: {exc}") from exc
        return self._text(resp)

    @staticmethod
    def _text(resp: Any) -> str:
        """Concatenate a response's text blocks (empty on a safety refusal).

        Shared by :meth:`generate` and :meth:`describe_image`: a refusal (or any
        non-text stop) leaves no usable content, so return empty and let the caller
        fall back rather than parsing a refusal string as if it were an answer.
        """
        if getattr(resp, "stop_reason", None) == "refusal":
            return ""
        parts = [
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts).strip()
