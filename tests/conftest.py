"""Shared test fixtures.

Keeps the suite hermetic with respect to Ollama. Some endpoints (calendar sync,
outing start) consult the local model through ``create_app``'s *default* client,
which points at ``localhost:11434``. On a developer machine that actually runs
Ollama that would make tests reach a live model — slow and non-deterministic. We
force the default client offline so behavior matches CI (no Ollama present).

Tests that exercise the model pass their own ``ollama=`` stub to ``create_app``;
that bypasses the default and is unaffected by this patch. ``test_summarizer``
imports :class:`OllamaClient` directly to test the client itself, so patching
only the ``app`` module's reference leaves it alone.
"""

from __future__ import annotations

import pytest

from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore, provision_user

#: Default handle every single-user test fixture provisions.
DEFAULT_HANDLE = "tester"


def scoped_default(store: MemoryStore, handle: str = DEFAULT_HANDLE) -> MemoryStore:
    """Provision ``handle`` on an unscoped store and return a store scoped to it.

    The single helper every existing single-tenant test fixture uses: the suite
    is multi-tenant now, so a store must be bound to a user before any per-user
    method works. Provisioning also seeds that user's coaching-state defaults
    (the per-user replacement for the old global ``schema.sql`` seed block), so a
    freshly fixtured user has exactly the state the tests expect.
    """
    user, _ = provision_user(store, handle, display_name=handle, is_operator=True)
    return store.scoped(user["id"])


class _OfflineOllama:
    """A stand-in Ollama client that is never available and never generates."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @classmethod
    def from_settings(cls, *args: object, **kwargs: object) -> "_OfflineOllama":
        return cls()

    def available(self) -> bool:
        return False

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        raise OllamaError("offline in tests")


@pytest.fixture(autouse=True)
def _offline_default_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``create_app``'s default Ollama client offline for every test."""
    # Fetch the module from sys.modules: the dotted-path / ``import ... as`` forms
    # are ambiguous because ``prefrontal.webhooks`` exposes a FastAPI ``app``
    # attribute that shadows the ``app`` submodule.
    import sys

    import prefrontal.webhooks.app  # noqa: F401 — ensure it's imported

    module = sys.modules["prefrontal.webhooks.app"]
    monkeypatch.setattr(module, "OllamaClient", _OfflineOllama)
