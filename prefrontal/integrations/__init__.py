"""Outbound and inbound integrations with external systems.

Houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`), the local Ollama inference client
(:mod:`prefrontal.integrations.ollama`), the optional Claude/Anthropic provider
(:mod:`prefrontal.integrations.anthropic`) used by the dashboard assistant, and
the native delivery client (:mod:`prefrontal.integrations.delivery`) that
publishes coaching decisions to Pushover / Ntfy / local TTS.

The delivery client is intentionally **not** re-exported here: it reaches up into
:mod:`prefrontal.webhooks` and :mod:`prefrontal.coaching`, whereas this package is
imported by low-level modules (e.g. :mod:`prefrontal.todos` pulls
:class:`~prefrontal.integrations.ollama.OllamaError`), so eager re-export would
create an import cycle. Import it directly: ``from prefrontal.integrations.delivery
import DeliveryClient``.
"""

from prefrontal.integrations.anthropic import AnthropicClient, AnthropicError
from prefrontal.integrations.n8n import N8nClient, N8nResult
from prefrontal.integrations.ollama import OllamaClient, OllamaError

__all__ = [
    "AnthropicClient",
    "AnthropicError",
    "N8nClient",
    "N8nResult",
    "OllamaClient",
    "OllamaError",
]
