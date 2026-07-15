"""Outbound and inbound integrations with external systems.

Houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`), the local Ollama inference client
(:mod:`prefrontal.integrations.ollama`), the optional Claude/Anthropic provider
(:mod:`prefrontal.integrations.anthropic`) used by the dashboard assistant, and
the native delivery client (:mod:`prefrontal.integrations.delivery`) that
publishes coaching decisions via native APNs push / a Twilio voice call / local
TTS (with a dev-only ntfy shim for free-signing builds).

The delivery client is intentionally **not** re-exported here: it reaches up into
:mod:`prefrontal.webhooks` and :mod:`prefrontal.coaching`, whereas this package is
imported by low-level modules (e.g. :mod:`prefrontal.todos` pulls
:class:`~prefrontal.integrations.ollama.OllamaError`), so eager re-export would
create an import cycle. Import it directly: ``from prefrontal.integrations.delivery
import DeliveryClient``.
"""

from typing import Protocol

from prefrontal.integrations.anthropic import AnthropicClient, AnthropicError
from prefrontal.integrations.base import ProviderError
from prefrontal.integrations.n8n import N8nClient, N8nResult
from prefrontal.integrations.ollama import OllamaClient, OllamaError
from prefrontal.integrations.provider import ProviderResolver


class Generator(Protocol):
    """The slice of an LLM client the domain layer calls: a single completion.

    Both :class:`~prefrontal.integrations.ollama.OllamaClient` and
    :class:`~prefrontal.integrations.anthropic.AnthropicClient` satisfy it
    structurally, so a call site can accept either without importing a concrete
    class. Defined here once instead of re-declared in each module that takes a
    client (the classifier, the todo augmenter, the trip/impulse/anchor inferers).
    """

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        num_ctx: int | None = None,
        timeout: float | None = None,
    ) -> str: ...


class ImageDescriber(Protocol):
    """The slice of a client the vision flow calls: read one image to text.

    Both :class:`~prefrontal.integrations.anthropic.AnthropicClient` (cloud) and
    :class:`~prefrontal.integrations.ollama.OllamaClient` (on-device) satisfy it,
    so :func:`prefrontal.vision.plan_vision` can accept whichever the provider
    selects without importing a concrete class. Kept separate from
    :class:`Generator` because reading pixels is a distinct capability from text
    completion — a text-only backend has ``generate`` but no ``describe_image``.
    """

    def available(self) -> bool: ...

    def describe_image(
        self,
        image_base64: str,
        *,
        prompt: str,
        media_type: str = ...,
        system: str | None = ...,
        max_tokens: int | None = ...,
    ) -> str: ...


__all__ = [
    "AnthropicClient",
    "AnthropicError",
    "Generator",
    "ImageDescriber",
    "N8nClient",
    "N8nResult",
    "OllamaClient",
    "OllamaError",
    "ProviderError",
    "ProviderResolver",
]
