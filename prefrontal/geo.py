"""Geographic primitives shared across location-aware features.

A neutral leaf for the pure geometry the location-anchor, departure, and trip
features all need — the great-circle distance and the default "you're home"
radius. Keeping them here means ``departure`` and ``trips`` no longer import the
``location_anchor`` *module* just to reach a formula (the coupling that forced
several lazy-import-to-dodge-a-cycle workarounds). Depends on nothing in the
package — only stdlib ``math``.
"""

from __future__ import annotations

import math
from typing import Any

#: Default radius (metres) within which the user counts as "home" — used to
#: suppress nudges and passively confirm a return when location is available.
DEFAULT_HOME_RADIUS_M = 150.0

#: Default radius (metres) within which a trip stop counts as "at" a curated
#: place — generous enough to absorb GPS scatter and the offset between a place's
#: saved pin and where the phone actually parked/dwelt, tight enough not to snap a
#: stop onto a place it merely drove past. Tunable via ``place_match_radius_m``.
DEFAULT_PLACE_MATCH_RADIUS_M = 200.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in metres.

    Used to annotate check responses with how far the user is from home; it does
    not affect escalation in v1.

    Args:
        lat1: Latitude of the first point (degrees).
        lon1: Longitude of the first point (degrees).
        lat2: Latitude of the second point (degrees).
        lon2: Longitude of the second point (degrees).

    Returns:
        Distance in metres.
    """
    radius = 6371000.0  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def nearest_place(
    places: list[dict[str, Any]],
    lat: float,
    lon: float,
    *,
    radius_m: float = DEFAULT_PLACE_MATCH_RADIUS_M,
) -> tuple[dict[str, Any], float] | None:
    """The curated place closest to ``(lat, lon)`` within ``radius_m``, and how far.

    The reverse of :func:`prefrontal.geocode.match_place` (which matches a place by
    *name*): this matches by *proximity*, for turning a coordinate — a closed-loop
    trip's stop — back into the place the user already named. Returns the single
    nearest place and its distance in metres, or ``None`` when the closest place is
    beyond ``radius_m`` (or there are no places). Pure — no store, no network.

    Args:
        places: Curated place dicts (each with ``lat``/``lon``; the rest is passed
            back untouched).
        lat: The query point's latitude.
        lon: The query point's longitude.
        radius_m: Match radius; a place farther than this doesn't count.

    Returns:
        ``(place, distance_m)`` for the nearest in-radius place, else ``None``.
    """
    best: tuple[dict[str, Any], float] | None = None
    for place in places:
        try:
            dist = haversine_m(lat, lon, float(place["lat"]), float(place["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if dist <= radius_m and (best is None or dist < best[1]):
            best = (place, dist)
    return best
