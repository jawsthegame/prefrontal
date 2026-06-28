"""Outbound and inbound integrations with external systems.

Houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`) and the local Ollama inference client
(:mod:`prefrontal.integrations.ollama`). Future delivery integrations (Pushover,
Ntfy, TTS) and an optional Anthropic provider will live here too.
"""

from prefrontal.integrations.n8n import N8nClient, N8nResult
from prefrontal.integrations.ollama import OllamaClient, OllamaError

__all__ = ["N8nClient", "N8nResult", "OllamaClient", "OllamaError"]
