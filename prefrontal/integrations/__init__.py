"""Outbound and inbound integrations with external systems.

Houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`), the local Ollama inference client
(:mod:`prefrontal.integrations.ollama`), and the optional Claude/Anthropic
provider (:mod:`prefrontal.integrations.anthropic`) used by the dashboard
assistant. Future delivery integrations (Pushover, Ntfy, TTS) will live here too.
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
