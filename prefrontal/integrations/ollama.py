"""Minimal client for a local Ollama server.

Ollama is Prefrontal's local inference engine — keeping reasoning on the host is
the "local first" promise. This client wraps just the endpoints Prefrontal needs:
``/api/generate`` (a single completion, text or multimodal) and ``/api/tags`` (a
liveness/availability check that doubles as the installed-model list).

It also carries the **on-device vision** path — :meth:`OllamaClient.describe_image`
reads a photo with a local multimodal model (e.g. ``llava``, ``llama3.2-vision``),
so the vision-capture flow can stay on the host rather than reaching for the cloud
Anthropic model. It's gated on :meth:`can_describe_images` (a vision model is
configured *and* installed) so the provider can route to it only when it will
actually work, falling back to Anthropic otherwise.

It is deliberately tiny and synchronous: these run periodically, not in a hot
path, so simplicity beats streaming/async here. Errors surface as
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
        vision_model: Optional multimodal model name for :meth:`describe_image`
            (e.g. ``llava``). Empty means on-device vision is off — the vision
            flow falls back to the cloud Anthropic model.
        timeout: Per-request timeout in seconds (generation can be slow).
        transport: Optional ``httpx`` transport, primarily for tests.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        vision_model: str = "",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.vision_model = vision_model
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> OllamaClient:
        """Build a client from :class:`~prefrontal.config.Settings`."""
        resolved = settings or get_settings()
        return cls(
            base_url=resolved.ollama_url,
            model=resolved.ollama_model,
            vision_model=resolved.ollama_vision_model,
        )

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
        return self._installed_models() is not None

    def _installed_models(self) -> set[str] | None:
        """Return the set of installed model names, or ``None`` if unreachable.

        Reads ``/api/tags`` (the same call :meth:`available` uses for liveness).
        Never raises — a down server or a malformed body is ``None`` so callers
        treat it as "can't tell / not available".
        """
        try:
            with self._client() as client:
                resp = client.get("/api/tags")
                if not resp.is_success:
                    return None
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return set()
        return {m["name"] for m in models if isinstance(m, dict) and "name" in m}

    def can_describe_images(self) -> bool:
        """Whether on-device vision is usable: a vision model configured *and*
        installed, on a reachable server.

        Lets the provider route vision to the local model only when it will
        actually work, rather than committing to it and getting an empty read. A
        configured name without a tag (``llava``) matches an installed tag
        (``llava:latest``) and vice-versa.
        """
        if not self.vision_model:
            return False
        installed = self._installed_models()
        if not installed:
            return False
        if self.vision_model in installed:
            return True
        # Tolerate a missing/implicit ``:latest`` tag on either side.
        bases = {name.split(":", 1)[0] for name in installed}
        return self.vision_model.split(":", 1)[0] in bases

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

    def describe_image(
        self,
        image_base64: str,
        *,
        prompt: str,
        media_type: str = "image/jpeg",
        system: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Read an image with the local multimodal model, returning its text.

        The on-device counterpart to
        :meth:`~prefrontal.integrations.anthropic.AnthropicClient.describe_image`
        — same shape (the :class:`~prefrontal.integrations.ImageDescriber`
        protocol) so the vision flow can prefer the local model and fall back to
        the cloud without caring which answered. Ollama's ``/api/generate`` takes
        the image as a base64 string in its ``images`` array.

        Args:
            image_base64: The image bytes, base64-encoded (no ``data:`` prefix).
            prompt: What to do with the image (the transcription instruction).
            media_type: Accepted for protocol parity but unused — Ollama sniffs the
                format from the bytes itself.
            system: Optional system prompt.
            max_tokens: Optional output-token cap, mapped to Ollama's
                ``num_predict`` option. ``None`` leaves the model's default.
            timeout: Optional per-call timeout override (seconds); vision decode is
                slow, so bump this for large images.

        Returns:
            The model's response text (stripped).

        Raises:
            OllamaError: If no vision model is configured, on transport failure, a
                non-2xx status, or a malformed body — so callers can fall back.
        """
        if not self.vision_model:
            raise OllamaError(
                "No Ollama vision model configured (set OLLAMA_VISION_MODEL)."
            )
        if not image_base64:
            raise OllamaError("No image data provided.")
        payload: dict[str, Any] = {
            "model": self.vision_model,
            "prompt": prompt,
            "images": [image_base64],
            "stream": False,
        }
        if system:
            payload["system"] = system
        if max_tokens is not None:
            payload["options"] = {"num_predict": max_tokens}
        try:
            with self._client(timeout) as client:
                resp = client.post("/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama vision request failed: {exc}") from exc
        except ValueError as exc:  # JSON decode
            raise OllamaError(f"Ollama returned a non-JSON body: {exc}") from exc

        response = data.get("response")
        if not isinstance(response, str):
            raise OllamaError("Ollama response missing a 'response' string.")
        return response.strip()
