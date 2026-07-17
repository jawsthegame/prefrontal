"""Outbound and inbound integrations with external systems — the transport leaf.

Houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`), the local Ollama inference client
(:mod:`prefrontal.integrations.ollama`), the optional Claude/Anthropic provider
(:mod:`prefrontal.integrations.anthropic`) used by the dashboard assistant, and
the low-level transport wire clients (APNs :mod:`~prefrontal.integrations.apns`,
Twilio/SMS :mod:`~prefrontal.integrations.sms`, SMTP :mod:`~prefrontal.integrations.smtp`).

This package is a **leaf**: it's imported by low-level modules (e.g.
:mod:`prefrontal.todos` pulls :class:`~prefrontal.integrations.ollama.OllamaError`),
so nothing here may import up into the domain or web layers. The native delivery
*client* that composes these transports with a coaching :class:`~prefrontal.coaching.Decision`
and the notification button builder therefore lives one layer up, at
:mod:`prefrontal.delivery` — import it directly from there
(``from prefrontal.delivery import DeliveryClient``).
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
