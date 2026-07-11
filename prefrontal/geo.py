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

#: Default radius (metres) within which the user counts as "home" — used to
#: suppress nudges and passively confirm a return when location is available.
DEFAULT_HOME_RADIUS_M = 150.0


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
