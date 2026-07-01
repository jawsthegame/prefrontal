"""Tests for the n8n integration client.

Focuses on the construction seam (`from_settings`) and no-op/local-first mode,
which need no network — a client with no webhook URL never leaves the host.
"""

from __future__ import annotations

from prefrontal.config import Settings
from prefrontal.integrations.n8n import N8nClient


def test_from_settings_reads_url_and_token():
    """from_settings mirrors the other integration clients' construction seam."""
    settings = Settings(
        n8n_webhook_url="https://n8n.example/webhook/abc",
        n8n_webhook_token="secret",
    )
    client = N8nClient.from_settings(settings)
    assert client.webhook_url == "https://n8n.example/webhook/abc"
    assert client.token == "secret"
    assert client.enabled is True


def test_from_settings_no_url_is_disabled_noop():
    """An unconfigured URL yields a disabled client that drops events locally."""
    client = N8nClient.from_settings(Settings())
    assert client.enabled is False
    result = client.trigger("episode.logged", {"id": 1})
    assert result.delivered is False
    assert result.status_code is None
