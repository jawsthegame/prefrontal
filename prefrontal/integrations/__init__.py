"""Outbound and inbound integrations with external systems.

Currently houses the n8n workflow-orchestration integration
(:mod:`prefrontal.integrations.n8n`). Future delivery integrations (Pushover,
Ntfy, TTS) and inference providers (Ollama, Anthropic) will live here too.
"""

from prefrontal.integrations.n8n import N8nClient, N8nResult

__all__ = ["N8nClient", "N8nResult"]
