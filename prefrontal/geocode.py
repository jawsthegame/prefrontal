"""Resolve a commitment's free-text location to destination coordinates.

The departure reminder (``prefrontal/departure.py``) only estimates travel time
when a commitment has ``dest_lat``/``dest_lon``. Calendars give a free-text
``location`` ("Dentist, 123 Main St"), not coordinates — so this module turns
text into a point, with a deliberately layered, local-first strategy:

1. **Curated places** — a user-maintained alias table ("gym" → coords). Instant,
   offline, and covers recurring destinations. Matched by name against the
   location *and* the title.
2. **Geocode cache** — a prior lookup for the same normalized string, including a
   recorded *miss* so a bad address isn't retried every sync.
3. **Network geocoder** — a Nominatim-compatible service, consulted only on a
   cache miss and only when ``geocoding_enabled`` is on. The result (hit or miss)
   is cached.

Everything except the network step is pure and offline; the network step is
opt-in. A failure anywhere degrades to "no coordinates", and the departure
reminder simply falls back to the commitment's static ``lead_minutes``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from prefrontal.integrations.nominatim import GeocoderError
from prefrontal.memory.store import MemoryStore


def normalize_query(text: str) -> str:
    """Normalize a location/place string for matching and cache keys.

    Lowercases, strips punctuation to spaces, and collapses whitespace, so
    ``"123 Main St."`` and ``"123 main st"`` share a key.

    Args:
        text: Raw location or place text.

    Returns:
        The normalized string (possibly empty).
    """
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


class Geocoder(Protocol):
    """The slice of :class:`~prefrontal.integrations.nominatim.NominatimGeocoder` used here."""

    def geocode(self, query: str) -> tuple[float, float] | None: ...


@dataclass(frozen=True)
class GeocodeResult:
    """Outcome of resolving one location.

    Attributes:
        coords: ``(lat, lon)`` if resolved, else ``None``.
        basis: How it was resolved/why not — ``place``/``cache``/``cache_miss``/
            ``geocoded``/``not_found``/``disabled``/``error``/``empty``.
    """

    coords: tuple[float, float] | None
    basis: str


def match_place(
    places: list[dict], location: str | None, title: str | None = None
) -> tuple[float, float] | None:
    """Match a curated place against a commitment's location/title.

    A place matches when its normalized ``name`` appears as a whole-word phrase
    in the normalized location or title. ``places`` should be ordered
    longest-name-first (as :meth:`MemoryStore.places` returns) so the most
    specific alias wins.

    Args:
        places: Curated place dicts (each with ``name``/``lat``/``lon``).
        location: The commitment's free-text location.
        title: The commitment's title (also searched — "Gym session" matches gym).

    Returns:
        ``(lat, lon)`` of the first matching place, or ``None``.
    """
    haystack = f"{normalize_query(location or '')} {normalize_query(title or '')}".strip()
    if not haystack:
        return None
    padded = f" {haystack} "
    for place in places:
        name = place.get("name") or ""
        if name and f" {name} " in padded:
            return float(place["lat"]), float(place["lon"])
    return None


def resolve_location(
    store: MemoryStore,
    location: str | None,
    *,
    title: str | None = None,
    geocoder: Geocoder | None = None,
) -> GeocodeResult:
    """Resolve a location to coordinates via places → cache → geocoder.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        location: The commitment's free-text location.
        title: The commitment's title (helps curated-place matching).
        geocoder: A network geocoder, or ``None`` to stay fully offline (places +
            cache only). The caller passes ``None`` when geocoding is disabled.

    Returns:
        A :class:`GeocodeResult`.
    """
    if not location or not location.strip():
        return GeocodeResult(None, "empty")

    # 1. Curated places — instant and offline.
    placed = match_place(store.places(), location, title)
    if placed is not None:
        return GeocodeResult(placed, "place")

    # 2. Cache — including a recorded miss (so we don't retry forever).
    query = normalize_query(location)
    cached = store.get_geocode_cache(query)
    if cached is not None:
        if cached["lat"] is not None and cached["lon"] is not None:
            return GeocodeResult((cached["lat"], cached["lon"]), "cache")
        return GeocodeResult(None, "cache_miss")

    # 3. Network geocoder — opt-in; cache whatever it definitively returns.
    if geocoder is None:
        return GeocodeResult(None, "disabled")
    try:
        coords = geocoder.geocode(location)
    except GeocoderError:
        # Transport/HTTP failure: don't cache, so a later sync retries.
        return GeocodeResult(None, "error")
    if coords is None:
        store.set_geocode_cache(query, None, None)  # definitive miss
        return GeocodeResult(None, "not_found")
    store.set_geocode_cache(query, coords[0], coords[1])
    return GeocodeResult(coords, "geocoded")


def enrich_commitments(
    store: MemoryStore,
    *,
    geocoder: Geocoder | None = None,
    limit: int = 25,
) -> dict[str, int]:
    """Fill in missing destination coordinates for upcoming commitments.

    Runs :func:`resolve_location` over commitments that have a ``location`` but no
    ``dest_lat``/``dest_lon`` and writes back any coordinates found. Bounded by
    ``limit`` so a single sync never makes an unbounded number of network calls;
    cached and curated-place hits cost nothing, so steady-state is cheap.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        geocoder: A network geocoder, or ``None`` for offline (places/cache only).
        limit: Maximum commitments to consider in this pass.

    Returns:
        Counts: ``{"considered", "resolved"}``.
    """
    pending = store.commitments_needing_geocode(limit=limit)
    resolved = 0
    for commitment in pending:
        result = resolve_location(
            store,
            commitment.get("location"),
            title=commitment.get("title"),
            geocoder=geocoder,
        )
        if result.coords is not None:
            store.set_commitment_coords(
                commitment["id"], result.coords[0], result.coords[1]
            )
            resolved += 1
    return {"considered": len(pending), "resolved": resolved}
