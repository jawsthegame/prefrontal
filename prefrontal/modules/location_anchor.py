"""Location-Aware Task Anchor module ("Coffee Shop Nudge").

When the user leaves home with a stated intention and a time estimate, the agent
tracks elapsed time against that estimate and escalates nudges to pull them back
to the original mission before they drift too far:

- **50%** of the stated window  → a *soft* push ("you've been out 7 min — on track?")
- **100%** of the stated window → a *firm* push ("it's been 15 min — wrap up")
- **150%** of the stated window → a *voice call* (Twilio) escalation

This file holds the pure, testable core: the escalation-level function, a
natural-language time-window parser, the message templates, and a distance
helper. The HTTP surface (``/webhooks/outing/{start,check,return}``) lives in
:mod:`prefrontal.webhooks.app` and the polling/delivery (Pushover + Twilio) lives
in the bundled n8n workflow. State is the ``outings`` table (see
:mod:`prefrontal.memory.store`).

Per the v1 spec, escalation is driven purely by **elapsed time** as a fraction of
the stated window; location is recorded for context/learning but does not gate
the nudges. AI interpretation of whether drift is acceptable is out of scope.
"""

from __future__ import annotations

import math
import re

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Escalation thresholds as fractions of the stated time window.
SOFT_THRESHOLD = 0.5
FIRM_THRESHOLD = 1.0
CALL_THRESHOLD = 1.5

#: Escalation levels in increasing severity. Index is the rank.
LEVELS = ("none", "soft", "firm", "call")

#: Default radius (metres) within which the user counts as "home" — used to
#: suppress nudges and passively confirm a return when location is available.
DEFAULT_HOME_RADIUS_M = 150.0

#: Default multiple of the stated window after which an unreturned outing is
#: auto-closed as abandoned (stops it lingering active forever).
DEFAULT_ABANDON_RATIO = 3.0


def level_rank(level: str) -> int:
    """Return the severity rank of an escalation level (``none`` -> 0)."""
    return LEVELS.index(level)


def is_at_home(distance_m: float | None, radius_m: float = DEFAULT_HOME_RADIUS_M) -> bool:
    """Whether a known distance from home is within the home radius.

    Args:
        distance_m: Distance from home in metres, or ``None`` if location is
            unknown (in which case the answer is ``False`` — we never assume).
        radius_m: The home radius in metres.

    Returns:
        ``True`` only when ``distance_m`` is known and within ``radius_m``.
    """
    return distance_m is not None and distance_m <= radius_m


def is_abandoned(
    elapsed_minutes: float, window_minutes: float, ratio: float = DEFAULT_ABANDON_RATIO
) -> bool:
    """Whether an outing has run long enough to be considered abandoned.

    Args:
        elapsed_minutes: Minutes since departure.
        window_minutes: The stated window.
        ratio: Multiple of the window beyond which it's abandoned.

    Returns:
        ``True`` if elapsed has reached ``window_minutes * ratio``.
    """
    return window_minutes > 0 and elapsed_minutes >= window_minutes * ratio


def escalation_level(elapsed_minutes: float, window_minutes: float) -> str:
    """Return the escalation level for an elapsed time against a window.

    Args:
        elapsed_minutes: Minutes since departure.
        window_minutes: The stated "back in N minutes" window.

    Returns:
        One of ``none``/``soft``/``firm``/``call``. ``none`` if the window is
        not positive (nothing to escalate against).
    """
    if window_minutes <= 0:
        return "none"
    ratio = elapsed_minutes / window_minutes
    if ratio >= CALL_THRESHOLD:
        return "call"
    if ratio >= FIRM_THRESHOLD:
        return "firm"
    if ratio >= SOFT_THRESHOLD:
        return "soft"
    return "none"


def build_message(
    level: str,
    *,
    elapsed_minutes: float,
    window_minutes: float,
    name: str = "",
) -> str:
    """Build the user-facing message for an escalation level.

    Args:
        level: ``soft``/``firm``/``call``.
        elapsed_minutes: Minutes since departure (rendered as a whole number).
        window_minutes: The stated window (rendered as a whole number).
        name: Optional first name; included in the voice-call message when set.

    Returns:
        The message string. Empty for ``none``/unknown levels.
    """
    elapsed = round(elapsed_minutes)
    window = round(window_minutes)
    greeting = f"Hey {name}" if name else "Hey"
    if level == "soft":
        return f"{greeting}, you've been out {elapsed} minutes — still on track?"
    if level == "firm":
        return f"It's been {window} minutes — time to wrap up and head home."
    if level == "call":
        return (
            f"{greeting}, you've been out {elapsed} minutes — you said you'd be "
            "back by now. Time to wrap up."
        )
    return ""


# Words/phrases the time-window parser understands beyond plain digits.
_WORD_WINDOWS = {
    "half an hour": 30.0,
    "half hour": 30.0,
    "quarter of an hour": 15.0,
    "quarter hour": 15.0,
    "an hour": 60.0,
    "a hour": 60.0,
    "a couple minutes": 2.0,
    "a few minutes": 5.0,
}
_NUM_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b",
    re.IGNORECASE,
)


def parse_time_window(text: str) -> float | None:
    """Extract a time window in minutes from a natural-language intention.

    Handles forms like "back in 15 minutes", "15 min", "in 1.5 hours", "an
    hour", "half an hour". Returns ``None`` when no window can be found so the
    caller can ask explicitly (per the spec: "extracted from intention or asked
    explicitly").

    Args:
        text: The stated intention.

    Returns:
        Minutes as a float, or ``None`` if nothing parseable was found.
    """
    lowered = text.lower()
    for phrase, minutes in _WORD_WINDOWS.items():
        if phrase in lowered:
            return minutes
    match = _NUM_UNIT.search(lowered)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("h"):
            return value * 60.0
        return value
    return None


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


class LocationAnchorModule(Module):
    """Nudges the user back to a stated mission as their time window elapses."""

    key = "location_anchor"
    title = "Location-Aware Task Anchor"
    challenge = (
        "Leaving with a short, stated intention and losing track of time — "
        "errands that quietly stretch well past the time you meant to be out."
    )
    default_state = {
        # Optional first name used in the voice-call message; blank by default.
        "user_name": "",
        # Radius (metres) within which the user counts as home (location-gating).
        "home_radius_m": str(int(DEFAULT_HOME_RADIUS_M)),
        # Auto-close an outing as abandoned past this multiple of its window.
        "abandon_after_ratio": str(DEFAULT_ABANDON_RATIO),
    }

    def interventions(self) -> list[Intervention]:
        """Declare the three-stage escalation; all are wired via n8n + Twilio."""
        return [
            Intervention(
                name="soft_nudge",
                description="Soft push at 50% of the stated window ('still on track?').",
                trigger="elapsed time reaches 50% of the stated window",
                status="active",
            ),
            Intervention(
                name="firm_nudge",
                description="Firm push at 100% of the stated window ('time to wrap up').",
                trigger="elapsed time reaches 100% of the stated window",
                status="active",
            ),
            Intervention(
                name="voice_call",
                description="Twilio voice call at 150% of the stated window.",
                trigger="elapsed time reaches 150% of the stated window",
                status="active",
            ),
            Intervention(
                name="location_gating",
                description="Suppress nudges and passively close the outing when home.",
                trigger="a location check places the user within the home radius",
                status="active",
            ),
            Intervention(
                name="abandoned_auto_close",
                description="Auto-close an outing left open far past its window.",
                trigger="elapsed time exceeds the abandon ratio with no return",
                status="active",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize recent errand punctuality from closed outings."""
        outings = [o for o in store.recent_outings(limit=100) if o.get("returned_at")]
        lines: list[str] = []
        if outings:
            over = 0
            for o in outings:
                # Recompute actual minutes from the stored timestamps.
                actual = _minutes_between(o.get("departure_at"), o.get("returned_at"))
                if actual is not None and actual > (o.get("time_window_minutes") or 0):
                    over += 1
            rate = over / len(outings)
            lines.append(
                f"Errand punctuality: {over}/{len(outings)} recent outings ran over "
                f"the stated window ({rate:.0%})."
            )
        lines.append(
            "Escalation path for active outings: soft push at 50%, firm push at "
            "100%, voice call at 150% of the stated time."
        )
        return "\n".join(f"- {line}" for line in lines)


def record_outing_return(store: MemoryStore, closed: dict) -> dict:
    """Log a genuine outing return as a ``task`` episode for pattern tracking.

    Predicted = the stated window, actual = minutes actually out, outcome
    ``success`` if within the window else ``miss`` — so the learning pass folds
    it into ``time_estimation`` and the bias.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        closed: A closed outing dict (with ``actual_minutes``).

    Returns:
        ``{"episode_id": int|None, "outcome": str|None}``.
    """
    actual = closed.get("actual_minutes")
    window = closed.get("time_window_minutes")
    if actual is None or window is None:
        return {"episode_id": None, "outcome": None}
    outcome = "success" if actual <= window else "miss"
    episode_id = store.log_episode(
        "task",
        predicted_value=window,
        actual_value=round(actual, 1),
        acknowledged=True,
        context=f"outing: {closed.get('intention')}",
        outcome=outcome,
    )
    return {"episode_id": episode_id, "outcome": outcome}


def record_outing_abandoned(store: MemoryStore, closed: dict) -> dict:
    """Log an abandoned outing as a drift ``miss`` (no reliable actual time).

    ``actual_value`` is intentionally ``None`` — we don't know when (or if) the
    user returned — so it contributes to ``drift`` but never pollutes
    ``time_estimation`` with a fabricated duration.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        closed: The closed (abandoned) outing dict.

    Returns:
        ``{"episode_id": int, "outcome": "miss"}``.
    """
    episode_id = store.log_episode(
        "task",
        predicted_value=closed.get("time_window_minutes"),
        actual_value=None,
        acknowledged=False,
        context=f"outing abandoned: {closed.get('intention')}",
        outcome="miss",
    )
    return {"episode_id": episode_id, "outcome": "miss"}


def _minutes_between(start: str | None, end: str | None) -> float | None:
    """Best-effort minutes between two ``YYYY-MM-DD HH:MM:SS`` UTC timestamps."""
    if not start or not end:
        return None
    from datetime import datetime

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        # Trim fractional seconds if SQLite included them.
        delta = datetime.strptime(end[:19], fmt) - datetime.strptime(start[:19], fmt)
    except ValueError:
        return None
    return delta.total_seconds() / 60.0


register(LocationAnchorModule())
