"""Departure planning — when to leave to make an upcoming commitment.

The Location-Aware Task Anchor handles "you're out, come back." This is the
mirror image: "you're home, it's time to leave." Given where the user is right
now (supplied by an iOS Shortcut via ``/webhooks/location``), a commitment's
destination coordinates, and the learned time bias, it estimates travel time and
decides when to nudge the user out the door.

Travel time is a deliberately crude but **fully local** estimate: straight-line
(haversine) distance × a road-factor ÷ an assumed speed, then padded by the same
``time_estimation_bias`` the rest of the system learns and a small prep buffer —
so it self-corrects as the user logs real departures, no routing API required.
When a commitment has no coordinates (or there is no recent location), it falls
back to the commitment's static ``lead_minutes``.

As the leave-by time approaches it escalates through three levels, mirroring the
anchor's soft/firm/call shape:

- **heads_up** — leave time is within ``departure_heads_up_minutes`` (a gentle
  "this is coming up").
- **soon** — leave time is within ``departure_soon_minutes`` (start getting ready).
- **go** — at or past the leave-by time (head out now, or you're already late).

The mirror of the *prediction* is the *outcome*: did the user actually leave on
time? An actual-departure signal — an iOS "when I leave Home" geofence hitting
``/webhooks/departure/left`` — is attributed to the commitment it was for
(:func:`attribute_departure`), compared against that commitment's computed
leave-by (:func:`classify_departure`), and logged as a ``departure`` episode
(:func:`record_departure_outcome`) so the learning pass finally sees whether
departures land on time. ``actual_value`` is deliberately left ``None`` (like the
abandoned-outing and closed-todo captures) so the outcome feeds the ``drift``
score without polluting the shared ``time_estimation_bias`` — leaving late must
not *lower* the underestimate multiplier.

Pure functions only; the HTTP surface (``/webhooks/departure/check`` and
``/webhooks/departure/left``) wires them to live data and handles dedup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from prefrontal.impact import utcnow
from prefrontal.modules.location_anchor import haversine_m

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: Assumed average travel speed (km/h) for the straight-line estimate. A mixed
#: urban default; tunable via the ``travel_speed_kmh`` coaching-state key.
DEFAULT_TRAVEL_SPEED_KMH = 30.0

#: Multiplier turning straight-line distance into rough road distance (roads
#: don't go as the crow flies). Tunable via ``travel_road_factor``.
DEFAULT_ROAD_FACTOR = 1.3

#: Minutes added to the travel estimate for getting out the door / parking.
DEFAULT_PREP_MINUTES = 5.0

#: Leave-by horizons (minutes) for the two pre-departure nudge levels.
DEFAULT_HEADS_UP_MINUTES = 30.0
DEFAULT_SOON_MINUTES = 10.0

#: Departure nudge levels in increasing urgency. Index is the rank.
LEVELS = ("none", "heads_up", "soon", "go")


def level_rank(level: str) -> int:
    """Return the urgency rank of a departure level (``none`` -> 0)."""
    return LEVELS.index(level)


def _parse(ts: str) -> datetime:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` UTC timestamp (naive UTC)."""
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


def estimate_travel_minutes(
    distance_m: float,
    speed_kmh: float = DEFAULT_TRAVEL_SPEED_KMH,
    road_factor: float = DEFAULT_ROAD_FACTOR,
) -> float:
    """Estimate travel time in minutes from a straight-line distance.

    ``road_distance = distance_m × road_factor``; minutes = that distance at
    ``speed_kmh``. No bias is applied here — the caller pads with the learned
    bias so the raw geometric estimate stays separable.

    Args:
        distance_m: Straight-line (haversine) distance in metres.
        speed_kmh: Assumed average speed in km/h (must be positive).
        road_factor: Straight-line → road-distance multiplier.

    Returns:
        Estimated travel minutes (``0.0`` if the speed is not positive).
    """
    if speed_kmh <= 0:
        return 0.0
    road_m = distance_m * road_factor
    return road_m / (speed_kmh * 1000.0 / 60.0)


def departure_level(
    minutes_until_leave: float,
    heads_up_minutes: float = DEFAULT_HEADS_UP_MINUTES,
    soon_minutes: float = DEFAULT_SOON_MINUTES,
) -> str:
    """Map "minutes until you must leave" to a nudge level.

    Args:
        minutes_until_leave: Minutes from now until the leave-by time; negative
            means the leave-by time has already passed.
        heads_up_minutes: Horizon for the gentle "heads up" level.
        soon_minutes: Horizon for the "get ready" level.

    Returns:
        One of ``none``/``heads_up``/``soon``/``go``.
    """
    if minutes_until_leave <= 0:
        return "go"
    if minutes_until_leave <= soon_minutes:
        return "soon"
    if minutes_until_leave <= heads_up_minutes:
        return "heads_up"
    return "none"


@dataclass(frozen=True)
class DeparturePlan:
    """When (and how urgently) to leave for one commitment.

    Attributes:
        commitment: The commitment dict.
        leave_by: UTC text — the latest you can leave and still arrive on time.
        minutes_until_leave: Minutes from now until ``leave_by`` (negative = late).
        travel_minutes: Bias-adjusted travel estimate, or ``None`` when distance
            could not be computed (fell back to ``lead_minutes``).
        basis: ``"distance"`` (estimated from coordinates) or ``"lead"`` (used the
            commitment's static ``lead_minutes``).
        level: The nudge level — ``none``/``heads_up``/``soon``/``go``.
    """

    commitment: dict[str, Any]
    leave_by: str
    minutes_until_leave: float
    travel_minutes: float | None
    basis: str
    level: str


def plan_departure(
    commitment: dict[str, Any],
    *,
    current_lat: float | None = None,
    current_lon: float | None = None,
    bias: float = 1.0,
    speed_kmh: float = DEFAULT_TRAVEL_SPEED_KMH,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    prep_minutes: float = DEFAULT_PREP_MINUTES,
    heads_up_minutes: float = DEFAULT_HEADS_UP_MINUTES,
    soon_minutes: float = DEFAULT_SOON_MINUTES,
    now: datetime | None = None,
) -> DeparturePlan:
    """Compute the departure plan for a single commitment.

    The leave-by time is ``start_at − buffer``. The buffer is the bias-adjusted
    travel estimate plus a prep allowance when both the current location and the
    commitment's destination coordinates are known; otherwise it is the
    commitment's static ``lead_minutes``.

    Args:
        commitment: A commitment dict (needs ``start_at``; uses ``dest_lat``/
            ``dest_lon`` and ``lead_minutes`` when present).
        current_lat: Current latitude, or ``None`` if location is unknown.
        current_lon: Current longitude, or ``None`` if location is unknown.
        bias: ``time_estimation_bias`` multiplier applied to the travel estimate.
        speed_kmh: Assumed average travel speed.
        road_factor: Straight-line → road-distance multiplier.
        prep_minutes: Minutes added to travel for getting out the door.
        heads_up_minutes: Horizon for the ``heads_up`` level.
        soon_minutes: Horizon for the ``soon`` level.
        now: Current naive-UTC time (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`DeparturePlan`.
    """
    now = now or utcnow()
    start = _parse(commitment["start_at"])

    dest_lat = commitment.get("dest_lat")
    dest_lon = commitment.get("dest_lon")
    if (
        current_lat is not None
        and current_lon is not None
        and dest_lat is not None
        and dest_lon is not None
    ):
        distance_m = haversine_m(current_lat, current_lon, dest_lat, dest_lon)
        travel = estimate_travel_minutes(distance_m, speed_kmh, road_factor) * bias
        buffer = travel + prep_minutes
        basis = "distance"
    else:
        travel = None
        buffer = commitment.get("lead_minutes") or 0.0
        basis = "lead"

    leave_by = start - timedelta(minutes=buffer)
    minutes_until = round((leave_by - now).total_seconds() / 60.0, 1)
    level = departure_level(minutes_until, heads_up_minutes, soon_minutes)
    return DeparturePlan(
        commitment=commitment,
        leave_by=leave_by.strftime("%Y-%m-%d %H:%M:%S"),
        minutes_until_leave=minutes_until,
        travel_minutes=round(travel, 1) if travel is not None else None,
        basis=basis,
        level=level,
    )


def next_departure(plans: list[DeparturePlan]) -> DeparturePlan | None:
    """Return the most urgent reminder-worthy plan, soonest leave-by first.

    Args:
        plans: Departure plans (typically one per upcoming commitment).

    Returns:
        The plan with the smallest ``minutes_until_leave`` among those whose
        level is not ``none``, or ``None`` if none are reminder-worthy.
    """
    worthy = [p for p in plans if p.level != "none"]
    if not worthy:
        return None
    return min(worthy, key=lambda p: p.minutes_until_leave)


def build_departure_message(plan: DeparturePlan, name: str = "") -> str:
    """Build the user-facing nudge for a departure plan.

    Args:
        plan: The :class:`DeparturePlan` to phrase.
        name: Optional first name, used as a light greeting when set.

    Returns:
        The message string. Empty for the ``none`` level.
    """
    title = plan.commitment.get("title") or "your next commitment"
    location = plan.commitment.get("location")
    where = f" ({location})" if location else ""
    travel = (
        f" (~{round(plan.travel_minutes)} min travel)"
        if plan.travel_minutes is not None
        else ""
    )
    greeting = f"Hey {name}, " if name else ""
    minutes = plan.minutes_until_leave

    if plan.level == "heads_up":
        return (
            f"{greeting}heads up: {title}{where} is coming up — plan to leave in "
            f"about {round(minutes)} min{travel}."
        )
    if plan.level == "soon":
        return (
            f"{greeting}time to get ready — leave for {title}{where} in about "
            f"{round(minutes)} min{travel}."
        )
    if plan.level == "go":
        if minutes < 0:
            return (
                f"{greeting}head out now for {title}{where} — you're about "
                f"{round(-minutes)} min past your leave time."
            )
        return f"{greeting}leave now for {title}{where}{travel}."
    return ""


# --- Departure outcome capture (did you actually leave on time?) -------------
#
# The prediction above (leave_by / level) is only half the learning loop; the
# other half is the *outcome*. An actual-departure signal (a "leave Home"
# geofence) is attributed to the commitment it was for, classified on-time vs
# late against that commitment's leave_by, and logged as a `departure` episode.

#: Minutes past leave-by tolerated before a departure counts as late.
DEFAULT_DEPARTURE_GRACE_MINUTES = 3.0

#: How early before leave-by a departure still plausibly belongs to a commitment
#: (leaving 3 hours before your evening thing isn't "leaving for" it).
DEFAULT_ATTRIBUTION_EARLY_MINUTES = 120.0

#: How late after start-time a departure still attributes (you left, just late).
DEFAULT_ATTRIBUTION_LATE_MINUTES = 30.0


def attribute_departure(
    plans: list[DeparturePlan],
    departed_at: datetime,
    *,
    early_minutes: float = DEFAULT_ATTRIBUTION_EARLY_MINUTES,
    late_minutes: float = DEFAULT_ATTRIBUTION_LATE_MINUTES,
) -> DeparturePlan | None:
    """Pick the commitment an actual departure was *for*.

    A departure plausibly belongs to a commitment when it happens inside that
    commitment's leave window — from ``early_minutes`` before its leave-by to
    ``late_minutes`` after its start (you can leave late). Among the plausible
    candidates the soonest-starting one wins: that's the obligation you're
    heading to. Returns ``None`` when nothing fits, so an errand unrelated to any
    commitment doesn't get logged as a (trivially on-time) departure.

    Args:
        plans: Departure plans, typically one per upcoming commitment.
        departed_at: When the user actually left (naive UTC).
        early_minutes: Widest lead before leave-by that still attributes.
        late_minutes: Grace after start-time that still attributes.

    Returns:
        The matched :class:`DeparturePlan`, or ``None``.
    """
    candidates: list[tuple[datetime, DeparturePlan]] = []
    for p in plans:
        start = _parse(p.commitment["start_at"])
        leave_by = _parse(p.leave_by)
        window_start = leave_by - timedelta(minutes=early_minutes)
        window_end = start + timedelta(minutes=late_minutes)
        if window_start <= departed_at <= window_end:
            candidates.append((start, p))
    if not candidates:
        return None
    candidates.sort(key=lambda sp: sp[0])
    return candidates[0][1]


def classify_departure(
    plan: DeparturePlan,
    departed_at: datetime,
    *,
    grace_minutes: float = DEFAULT_DEPARTURE_GRACE_MINUTES,
) -> tuple[str, float]:
    """Classify an actual departure against its plan's leave-by.

    Args:
        plan: The matched departure plan.
        departed_at: When the user actually left (naive UTC).
        grace_minutes: Minutes past leave-by still counted as on time.

    Returns:
        ``(outcome, lateness_minutes)`` — ``outcome`` is ``"success"`` (on time)
        or ``"miss"`` (late); ``lateness_minutes`` is positive when late,
        negative when the user left with time to spare.
    """
    leave_by = _parse(plan.leave_by)
    lateness = round((departed_at - leave_by).total_seconds() / 60.0, 1)
    outcome = "miss" if lateness > grace_minutes else "success"
    return outcome, lateness


def record_departure_outcome(
    store: MemoryStore,
    plan: DeparturePlan,
    departed_at: datetime,
    *,
    grace_minutes: float = DEFAULT_DEPARTURE_GRACE_MINUTES,
) -> dict[str, Any]:
    """Log an actual departure as a ``departure`` episode for pattern tracking.

    Outcome is ``success`` (left on time) or ``miss`` (left late), feeding the
    ``departure`` ``drift`` score. ``actual_value`` is intentionally ``None`` (as
    with abandoned outings and closed todos) so it never pollutes the shared
    ``time_estimation_bias``; the planned buffer is kept in ``predicted_value``
    and the human detail in ``notes``. The episode is stamped at ``departed_at``.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        plan: The matched departure plan.
        departed_at: When the user actually left (naive UTC).
        grace_minutes: Minutes past leave-by still counted as on time.

    Returns:
        ``{"episode_id", "outcome", "lateness_minutes", "commitment_id"}``.
    """
    outcome, lateness = classify_departure(plan, departed_at, grace_minutes=grace_minutes)
    start = _parse(plan.commitment["start_at"])
    leave_by = _parse(plan.leave_by)
    planned_buffer = round((start - leave_by).total_seconds() / 60.0, 1)
    hhmm = plan.leave_by[11:16]
    if outcome == "miss":
        notes = f"left ~{round(lateness)} min late (leave-by {hhmm})"
    else:
        spare = round(max(0.0, -lateness))
        notes = (
            f"left on time (~{spare} min to spare, leave-by {hhmm})"
            if spare >= 1
            else f"left right on time (leave-by {hhmm})"
        )
    episode_id = store.log_episode(
        "departure",
        predicted_value=planned_buffer,
        actual_value=None,
        acknowledged=True,
        context=f"auto departure: {plan.commitment.get('title') or 'commitment'}",
        outcome=outcome,
        notes=notes,
        timestamp=departed_at.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {
        "episode_id": episode_id,
        "outcome": outcome,
        "lateness_minutes": lateness,
        "commitment_id": plan.commitment.get("id"),
    }
