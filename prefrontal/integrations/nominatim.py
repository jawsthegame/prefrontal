"""Minimal client for a Nominatim-compatible geocoding service.

Geocoding is the one place Prefrontal reaches off-host, so it's opt-in (the
``geocoding_enabled`` coaching-state flag) and isolated here. This client wraps a
single endpoint — a forward-geocode search that turns a free-text address into a
``(lat, lon)`` point — and is deliberately tiny and synchronous, like
:class:`~prefrontal.integrations.ollama.OllamaClient`.

A *not found* result (the service answered, with no match) returns ``None`` and
is a normal outcome the caller caches as a definitive miss. A *transport/HTTP*
failure raises :class:`GeocoderError` so the caller can skip without caching, and
retry on a later sync.

The default target is the public OpenStreetMap Nominatim service, whose usage
policy requires an identifying ``User-Agent`` and at most ~1 request/second — so
callers should keep per-pass volume small and lean on the cache.
"""

from __future__ import annotations

import httpx

from prefrontal.config import Settings, get_settings


class GeocoderError(RuntimeError):
    """Raised when a geocoder request fails (transport error or non-2xx)."""


class NominatimGeocoder:
    """Synchronous forward-geocoder for a Nominatim-compatible endpoint.

    Args:
        url: Full search endpoint URL (e.g. the OSM Nominatim ``/search``).
        user_agent: ``User-Agent`` header — Nominatim's policy requires an
            identifying value.
        timeout: Per-request timeout in seconds.
        transport: Optional ``httpx`` transport, primarily for tests.
    """

    def __init__(
        self,
        url: str = "https://nominatim.openstreetmap.org/search",
        user_agent: str = "Prefrontal/0.1 (https://github.com/jawsthegame/prefrontal)",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> NominatimGeocoder:
        """Build a geocoder from :class:`~prefrontal.config.Settings`."""
        resolved = settings or get_settings()
        return cls(url=resolved.geocoder_url, user_agent=resolved.geocoder_user_agent)

    def geocode(self, query: str) -> tuple[float, float] | None:
        """Resolve a free-text location to ``(lat, lon)``, or ``None`` if unmatched.

        Args:
            query: A free-text address or place description.

        Returns:
            ``(lat, lon)`` for the top match, or ``None`` if the service returned
            no results (a definitive miss).

        Raises:
            GeocoderError: On transport failure, a non-2xx status, or a malformed
                response body — distinct from a clean "no match" (``None``).
        """
        params = {"q": query, "format": "json", "limit": "1"}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                resp = client.get(
                    self.url, params=params, headers={"User-Agent": self.user_agent}
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise GeocoderError(f"Geocoder request failed: {exc}") from exc
        except ValueError as exc:  # JSON decode
            raise GeocoderError(f"Geocoder returned a non-JSON body: {exc}") from exc

        if not isinstance(data, list) or not data:
            return None
        top = data[0]
        try:
            return float(top["lat"]), float(top["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocoderError(f"Geocoder result missing lat/lon: {exc}") from exc
