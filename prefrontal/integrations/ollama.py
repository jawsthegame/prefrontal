"""Minimal client for a local Ollama server.

Ollama is Prefrontal's local inference engine — keeping reasoning on the host is
the "local first" promise. This client wraps just the two endpoints the
summarizer needs: ``/api/generate`` (a single completion) and ``/api/tags`` (a
liveness/availability check).

It is deliberately tiny and synchronous: the summarizer runs periodically, not in
a hot path, so simplicity beats streaming/async here. Errors surface as
:class:`OllamaError` so callers (e.g. the summarizer) can fall back gracefully.
"""

from __future__ import annotations

from typing import Any

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.integrations.base import ProviderError


class OllamaError(ProviderError):
    """Raised when an Ollama request fails (transport error or non-2xx)."""


class OllamaClient:
    """Synchronous client for a local Ollama server.

    Args:
        base_url: Ollama server base URL (e.g. ``http://localhost:11434``).
        model: Default model name to generate with.
        timeout: Per-request timeout in seconds (generation can be slow).
        transport: Optional ``httpx`` transport, primarily for tests.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> OllamaClient:
        """Build a client from :class:`~prefrontal.config.Settings`."""
        resolved = settings or get_settings()
        return cls(base_url=resolved.ollama_url, model=resolved.ollama_model)

    def _client(self, timeout: float | None = None) -> httpx.Client:
        """Construct an ``httpx.Client`` bound to the configured base URL."""
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout if timeout is None else timeout,
            transport=self._transport,
        )

    def available(self) -> bool:
        """Return ``True`` if the server answers ``/api/tags`` successfully.

        Never raises — a down or unreachable server simply returns ``False`` so
        callers can decide whether to fall back.
        """
        try:
            with self._client() as client:
                return client.get("/api/tags").is_success
        except httpx.HTTPError:
            return False

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        num_ctx: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Generate a single non-streamed completion.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.
            num_ctx: Optional context-window size (tokens) for this call. Ollama's
                default (~2048) silently truncates a long prompt from the front, so
                a caller feeding a big transcript must raise this to fit it —
                otherwise the model only ever sees a sliver.
            timeout: Optional per-call timeout override (seconds); a large ``num_ctx``
                makes prompt evaluation much slower, so bump this alongside it.

        Returns:
            The model's response text (stripped).

        Raises:
            OllamaError: On transport failure, a non-2xx status, or a malformed
                response body.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}
        try:
            with self._client(timeout) as client:
                resp = client.post("/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        except ValueError as exc:  # JSON decode
            raise OllamaError(f"Ollama returned a non-JSON body: {exc}") from exc

        response = data.get("response")
        if not isinstance(response, str):
            raise OllamaError("Ollama response missing a 'response' string.")
        return response.strip()
