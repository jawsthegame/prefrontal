"""The shared-service bundle injected into every router factory.

:func:`prefrontal.webhooks.app.create_app` builds one :class:`RouterServices` and
hands the *same* instance to each router's ``build_router``. A router binds only
the fields it actually uses; several use none. Adding a shared service is then a
single new field here, not a signature change rippled across all thirteen
routers — and no router advertises a dependency it doesn't touch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from prefrontal.config import Settings
from prefrontal.integrations import (
    AnthropicClient,
    N8nClient,
    OllamaClient,
    ProviderResolver,
)
from prefrontal.integrations.nominatim import NominatimGeocoder


@dataclass(frozen=True)
class RouterServices:
    """Shared services built once by ``create_app`` and passed to every router.

    Attributes:
        settings: The resolved :class:`~prefrontal.config.Settings`.
        n8n: Outbound n8n client.
        summarizer: Ollama client with the longer generation timeout, for the
            heavier generations (profile summary, delegation-prep briefs). Kept
            separate from the snappy inference client because of its timeout;
            passed as the explicit local ``fallback`` for the ``summarizer`` agent.
        anthropic: Optional Claude client for the dashboard assistant.
        provider: Per-agent backend selector (Claude vs local). Routers resolve a
            selectable agent's client via ``provider.client("<agent>")``; a path
            that is deliberately local (todo/mail decompose, clarify, calendar
            classify, …) uses the local client directly via ``provider.ollama``.
            Either way the local inference client is reached through the provider,
            not a duplicate bundle field.
        geocoder: Forward geocoder (usually reached only via ``run_geocode``).
        run_geocode: Best-effort commitment-destination enrichment closure.
    """

    settings: Settings
    n8n: N8nClient
    summarizer: OllamaClient
    anthropic: AnthropicClient
    provider: ProviderResolver
    geocoder: NominatimGeocoder
    run_geocode: Callable[..., dict[str, int]]
