"""Departure planning — when to leave to make an upcoming commitment.

The Location-Aware Task Anchor handles "you're out, come back." This is the
mirror image: "you're home, it's time to leave." Given where the user is right
now (supplied by an iOS Shortcut via ``/webhooks/location``), a commitment's
destination coordinates, and the learned time bias, it estimates travel time and
decides when to nudge the user out the door.

Travel time is a deliberately crude but **fully local** estimate: straight-line
(haversine) distance × a road-factor ÷ an assumed speed, then padded by the same
``time_estimation_bias`` the rest of the system learns, an optional
distance-relative safety margin (``travel_pad_fraction`` — a percentage of the
drive, so longer trips get more cushion), and a small prep buffer — so it
self-corrects as the user logs real departures, no routing API required.
When a commitment has no coordinates (or there is no recent location), it falls
back to the commitment's static ``lead_minutes``.

As the leave-by time approaches it escalates through three levels, mirroring the
anchor's soft/firm/call shape:

- **heads_up** — leave time is within ``departure_heads_up_minutes`` (a gentle
  "this is coming up").
- **soon** — leave time is within ``departure_soon_minutes`` (start getting ready).
- **go** — at or past the leave-by time (head out now, or you're already late).

Not every commitment is somewhere you travel to. Work meetings are mostly
attended from wherever you already are (desk / WFH), so a commitment on an
attend-mode feed (:func:`departure_mode`) skips travel logic entirely: it fires a
single short "starts soon" reminder ``work_lead_minutes`` before start instead of
a "leave now" nudge. A ``[commute]`` tag on the meeting, or a flagged in-office
day, flips it back to the travel path.

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

from prefrontal.clock import TS_FMT, parse_ts
from prefrontal.clock import parse_ts_strict as _parse
from prefrontal.commitments import is_attendable
from prefrontal.geo import haversine_m
from prefrontal.impact import utcnow

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

#: Distance-relative safety padding on the travel estimate, as a fraction of the
#: estimated travel time (which is itself proportional to distance). ``0.15``
#: means "pad the drive by 15%", so a 40-minute trip gains 6 minutes and a
#: 10-minute one only 1.5 — the cushion grows with the distance, unlike the flat
#: ``prep_minutes``. Kept separate from ``time_estimation_bias`` (which is
#: *learned* and self-correcting) so an explicit safety margin never pollutes the
#: learned multiplier. Tunable via the ``travel_pad_fraction`` coaching-state key;
#: defaults to ``0.0`` (off) so it changes nothing until set.
DEFAULT_TRAVEL_PAD_FRACTION = 0.0

#: Coaching-state key holding :data:`DEFAULT_TRAVEL_PAD_FRACTION`'s override.
TRAVEL_PAD_FRACTION_KEY = "travel_pad_fraction"

#: Whether the learning pass may auto-populate ``travel_pad_fraction`` from the
#: departure late-rate. On by default; the Settings card can switch it off to
#: freeze the pad (a hand-set value already sticks regardless via its source).
TRAVEL_PAD_AUTOLEARN_KEY = "travel_pad_autolearn"
DEFAULT_TRAVEL_PAD_AUTOLEARN = True

#: Leave-by horizons (minutes) for the two pre-departure nudge levels.
DEFAULT_HEADS_UP_MINUTES = 30.0
DEFAULT_SOON_MINUTES = 10.0

#: Lead (minutes) for an **attend-mode** commitment — you're already where you'll
#: attend it (e.g. a work meeting taken from your desk), so there's nothing to
#: travel to and a single short "starts soon" reminder is all you want.
DEFAULT_WORK_LEAD_MINUTES = 5.0

#: Calendar feed slugs (``external_id`` namespaces) whose commitments default to
#: attend mode — "I'm already where I need to be." Tunable via ``attend_calendars``.
DEFAULT_ATTEND_SLUGS = ("work",)

#: Case-insensitive tags in a commitment's title/location that force an
#: attend-mode commitment *back* to travel mode — the rare work meeting you must
#: physically go to. Bracketed so they can't false-match ordinary words.
TRAVEL_TAGS = ("[commute]", "[in-person]", "[in person]", "[onsite]", "[on-site]")

#: Departure nudge levels in increasing urgency. Index is the rank.
LEVELS = ("none", "heads_up", "soon", "go")


def level_rank(level: str) -> int:
    """Return the urgency rank of a departure level (``none`` -> 0)."""
    return LEVELS.index(level)


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


def adjusted_travel_minutes(
    distance_m: float,
    *,
    speed_kmh: float = DEFAULT_TRAVEL_SPEED_KMH,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    bias: float = 1.0,
    pad_fraction: float = DEFAULT_TRAVEL_PAD_FRACTION,
) -> float:
    """Travel estimate with the learned bias and the distance-relative safety pad.

    The raw geometric estimate (:func:`estimate_travel_minutes`) times ``bias``
    (the learned ``time_estimation_bias``) times ``1 + pad_fraction`` (an explicit
    safety cushion that scales with the trip). Both departure surfaces route
    through here so a commitment is planned and scored against the same padded
    estimate. A non-positive ``pad_fraction`` is floored at ``0`` — padding only
    ever lengthens the estimate, it never trims it.
    """
    raw = estimate_travel_minutes(distance_m, speed_kmh, road_factor)
    return raw * bias * (1.0 + max(pad_fraction, 0.0))


def travel_leads(
    commitments: list[dict[str, Any]],
    current_lat: float | None,
    current_lon: float | None,
    *,
    bias: float = 1.0,
    speed_kmh: float = DEFAULT_TRAVEL_SPEED_KMH,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    prep_minutes: float = DEFAULT_PREP_MINUTES,
    pad_fraction: float = DEFAULT_TRAVEL_PAD_FRACTION,
) -> dict[int, float]:
    """Effective per-commitment lead times from real travel along the day's chain.

    Walks the commitments in start order as a sequence of legs: from your current
    location to the first, then location-to-location between consecutive ones. For
    each leg where both endpoints have coordinates, the lead is the bias- and
    pad-adjusted travel estimate (:func:`adjusted_travel_minutes`) plus a
    ``prep_minutes`` "out the door" buffer — the same formula the departure
    reminder uses. Legs with an unknown endpoint are omitted, so
    :func:`prefrontal.impact.cascade_impact` falls back to their static
    ``lead_minutes``.

    Returns a ``{commitment_id: minutes}`` map suitable for ``cascade_impact``'s
    ``lead_override``. Empty when nothing can be estimated (no location, or no
    destination coordinates anywhere).
    """
    prev: tuple[float, float] | None = (
        (current_lat, current_lon)
        if current_lat is not None and current_lon is not None
        else None
    )
    leads: dict[int, float] = {}
    for c in sorted(commitments, key=lambda x: _parse(x["start_at"])):
        dest_lat, dest_lon = c.get("dest_lat"), c.get("dest_lon")
        dest = (dest_lat, dest_lon) if dest_lat is not None and dest_lon is not None else None
        cid = c.get("id")
        if prev is not None and dest is not None and cid is not None:
            distance_m = haversine_m(prev[0], prev[1], dest[0], dest[1])
            travel = adjusted_travel_minutes(
                distance_m,
                speed_kmh=speed_kmh,
                road_factor=road_factor,
                bias=bias,
                pad_fraction=pad_fraction,
            )
            leads[cid] = round(travel + prep_minutes, 1)
        # Advance the chain to this destination when we know it; an unknown
        # location leaves ``prev`` as the last place we could pin down.
        if dest is not None:
            prev = dest
    return leads


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


def departure_mode(
    commitment: dict[str, Any],
    *,
    attend_slugs: tuple[str, ...] = DEFAULT_ATTEND_SLUGS,
    office_day: bool = False,
) -> str:
    """Decide whether a commitment is an *attend* or a *travel* departure.

    The default for everything is ``"travel"`` — estimate the trip and nudge the
    user out the door. Commitments on an attend-mode feed (``calendar_key`` in
    ``attend_slugs``, e.g. the ``work`` calendar) instead default to ``"attend"``:
    the user is already where they'll attend (WFH / at their desk), so no travel
    logic applies and a single short "starts soon" reminder is enough.

    Two things flip an attend-mode commitment back to ``"travel"``:

    - a :data:`TRAVEL_TAGS` marker in its title/location (``[commute]``) — this
      specific meeting is in person, and
    - ``office_day`` — the user flagged today as an in-office day, so *all* work
      commitments want the travel-aware "leave now" nudge again.

    Args:
        commitment: A commitment dict (uses ``calendar_key``, ``title``, ``location``).
        attend_slugs: Feed slugs that default to attend mode.
        office_day: Whether today is flagged as an in-office day.

    Returns:
        ``"attend"`` or ``"travel"``.
    """
    slug = (commitment.get("calendar_key") or "").lower()
    if slug not in attend_slugs:
        return "travel"
    text = f"{commitment.get('title') or ''} {commitment.get('location') or ''}".lower()
    if any(tag in text for tag in TRAVEL_TAGS):
        return "travel"
    if office_day:
        return "travel"
    return "attend"


@dataclass(frozen=True)
class DeparturePlan:
    """When (and how urgently) to leave for one commitment.

    Attributes:
        commitment: The commitment dict.
        leave_by: UTC text — the latest you can leave and still arrive on time.
        minutes_until_leave: Minutes from now until ``leave_by`` (negative = late).
        travel_minutes: Bias- and pad-adjusted travel estimate, or ``None`` when
            distance could not be computed (fell back to ``lead_minutes``).
        basis: ``"distance"`` (estimated from coordinates), ``"lead"`` (used the
            commitment's static ``lead_minutes``), or ``"attend"`` (a fixed short
            lead — you're already where you'll attend, no travel).
        level: The nudge level — ``none``/``heads_up``/``soon``/``go``.
        mode: ``"travel"`` (go somewhere) or ``"attend"`` (attend from here).
    """

    commitment: dict[str, Any]
    leave_by: str
    minutes_until_leave: float
    travel_minutes: float | None
    basis: str
    level: str
    mode: str = "travel"


def plan_departure(
    commitment: dict[str, Any],
    *,
    current_lat: float | None = None,
    current_lon: float | None = None,
    bias: float = 1.0,
    speed_kmh: float = DEFAULT_TRAVEL_SPEED_KMH,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    prep_minutes: float = DEFAULT_PREP_MINUTES,
    pad_fraction: float = DEFAULT_TRAVEL_PAD_FRACTION,
    heads_up_minutes: float = DEFAULT_HEADS_UP_MINUTES,
    soon_minutes: float = DEFAULT_SOON_MINUTES,
    work_lead_minutes: float = DEFAULT_WORK_LEAD_MINUTES,
    attend_slugs: tuple[str, ...] = DEFAULT_ATTEND_SLUGS,
    office_day: bool = False,
    now: datetime | None = None,
) -> DeparturePlan:
    """Compute the departure plan for a single commitment.

    First :func:`departure_mode` decides the shape of the plan:

    - **attend** (e.g. a work meeting you'll take from your desk) — no travel
      logic. The leave-by is ``start − work_lead_minutes`` and the plan fires a
      single reminder (level ``go``) once that lead is reached; ``basis`` is
      ``"attend"``. This is the "I'm already where I need to be" case.
    - **travel** (the default) — the leave-by is ``start − buffer``, where the
      buffer is the bias- and pad-adjusted travel estimate plus a prep allowance when both
      the current location and the destination coordinates are known, otherwise
      the commitment's static ``lead_minutes``. It escalates
      ``heads_up`` → ``soon`` → ``go`` as the leave-by approaches.

    Args:
        commitment: A commitment dict (needs ``start_at``; uses ``calendar_key``,
            ``dest_lat``/``dest_lon`` and ``lead_minutes`` when present).
        current_lat: Current latitude, or ``None`` if location is unknown.
        current_lon: Current longitude, or ``None`` if location is unknown.
        bias: ``time_estimation_bias`` multiplier applied to the travel estimate.
        speed_kmh: Assumed average travel speed.
        road_factor: Straight-line → road-distance multiplier.
        prep_minutes: Minutes added to travel for getting out the door.
        pad_fraction: Distance-relative safety padding on the travel estimate, as
            a fraction of the estimated travel time (e.g. ``0.15`` = +15%). Grows
            with the trip, unlike the flat ``prep_minutes``. ``0.0`` disables it.
        heads_up_minutes: Horizon for the ``heads_up`` level (travel mode).
        soon_minutes: Horizon for the ``soon`` level (travel mode).
        work_lead_minutes: Fixed lead for an attend-mode reminder.
        attend_slugs: Feed slugs that default to attend mode.
        office_day: Whether today is flagged as an in-office day (forces travel).
        now: Current naive-UTC time (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`DeparturePlan`.
    """
    now = now or utcnow()
    start = _parse(commitment["start_at"])
    mode = departure_mode(commitment, attend_slugs=attend_slugs, office_day=office_day)

    if mode == "attend":
        travel = None
        buffer = work_lead_minutes
        basis = "attend"
    else:
        dest_lat = commitment.get("dest_lat")
        dest_lon = commitment.get("dest_lon")
        if (
            current_lat is not None
            and current_lon is not None
            and dest_lat is not None
            and dest_lon is not None
        ):
            distance_m = haversine_m(current_lat, current_lon, dest_lat, dest_lon)
            travel = adjusted_travel_minutes(
                distance_m,
                speed_kmh=speed_kmh,
                road_factor=road_factor,
                bias=bias,
                pad_fraction=pad_fraction,
            )
            buffer = travel + prep_minutes
            basis = "distance"
        else:
            travel = None
            buffer = commitment.get("lead_minutes") or 0.0
            basis = "lead"

    leave_by = start - timedelta(minutes=buffer)
    minutes_until = round((leave_by - now).total_seconds() / 60.0, 1)
    # Attend mode is a single reminder at the lead, not the travel escalation
    # ladder — you're already there, so heads_up/soon "get ready to leave" is moot.
    if mode == "attend":
        level = "go" if minutes_until <= 0 else "none"
    else:
        level = departure_level(minutes_until, heads_up_minutes, soon_minutes)
    return DeparturePlan(
        commitment=commitment,
        leave_by=leave_by.strftime(TS_FMT),
        minutes_until_leave=minutes_until,
        travel_minutes=round(travel, 1) if travel is not None else None,
        basis=basis,
        level=level,
        mode=mode,
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

    The commitment's ``notes`` (if any) are consulted and folded onto the end as
    a ``Note: …`` hint (:func:`prefrontal.coaching.note_hint`), so a reminder that
    gets you out the door also carries the thing you'd otherwise forget to bring
    ("leave now for the dentist — Note: bring the insurance card").
    """
    # Local import keeps departure.py free of a module-load dependency on coaching
    # (which pulls in scheduling), matching the lazy imports elsewhere in this file.
    from prefrontal.coaching import note_hint

    title = plan.commitment.get("title") or "your next commitment"
    location = plan.commitment.get("location")
    where = f" ({location})" if location else ""
    travel = (
        f" (~{round(plan.travel_minutes)} min travel)"
        if plan.travel_minutes is not None
        else ""
    )
    note = note_hint(plan.commitment.get("notes"))
    greeting = f"Hey {name}, " if name else ""
    minutes = plan.minutes_until_leave

    # Attend mode: no travel, one "starts soon" reminder — never say "leave".
    if plan.mode == "attend":
        if plan.level != "go":
            return ""
        start = _parse(plan.commitment["start_at"])
        leave_by = _parse(plan.leave_by)
        lead = (start - leave_by).total_seconds() / 60.0
        mins_to_start = max(0, round(lead + minutes))
        if mins_to_start <= 0:
            return f"{greeting}{title}{where} is starting now.{note}"
        return (
            f"{greeting}{title}{where} starts in about {mins_to_start} min — "
            f"you're already where you need to be.{note}"
        )

    if plan.level == "heads_up":
        return (
            f"{greeting}heads up: {title}{where} is coming up — plan to leave in "
            f"about {round(minutes)} min{travel}.{note}"
        )
    if plan.level == "soon":
        return (
            f"{greeting}time to get ready — leave for {title}{where} in about "
            f"{round(minutes)} min{travel}.{note}"
        )
    if plan.level == "go":
        if minutes < 0:
            return (
                f"{greeting}head out now for {title}{where} — you're about "
                f"{round(-minutes)} min past your leave time.{note}"
            )
        return f"{greeting}leave now for {title}{where}{travel}.{note}"
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
    tz: str = "UTC",
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
        tz: The user's IANA timezone, used only to render the human-readable
            leave-by clock time in the note (timestamps are stored naive UTC).

    Returns:
        ``{"episode_id", "outcome", "lateness_minutes", "commitment_id"}``.
    """
    from prefrontal.clock import local_datetime

    outcome, lateness = classify_departure(plan, departed_at, grace_minutes=grace_minutes)
    start = _parse(plan.commitment["start_at"])
    leave_by = _parse(plan.leave_by)
    planned_buffer = round((start - leave_by).total_seconds() / 60.0, 1)
    # `leave_by` is naive UTC; the note is user-facing, so render its local
    # clock time rather than slicing HH:MM straight out of the UTC string.
    hhmm = local_datetime(leave_by, tz).strftime("%H:%M")
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
        timestamp=departed_at.strftime(TS_FMT),
    )
    return {
        "episode_id": episode_id,
        "outcome": outcome,
        "lateness_minutes": lateness,
        "commitment_id": plan.commitment.get("id"),
    }


# --- Orchestration: the departure-check decision (web + CLI share this) ------
#
# ``plan_departure``/``next_departure`` above are pure. This is the *decision*
# the ``/webhooks/departure/check`` endpoint used to inline: read the tuning and
# current location from the store, plan every upcoming commitment, pick the most
# urgent, and — debounced by a ``(commitment, level)`` signature so a standing
# reminder doesn't re-alert every poll — decide whether to fire and record the
# nudge. Extracted here (like household.run_chores_check) so it is unit-testable
# and a CLI twin can share one path; the router only body-parses and decorates
# the reminder with its one-tap HTTP links.


def _attend_slugs(store: MemoryStore) -> tuple[str, ...]:
    """Calendar feed slugs that default to attend mode (from ``attend_calendars``).

    A comma-separated coaching-state string (default ``"work"``) → a lowercased
    tuple. Falls back to :data:`DEFAULT_ATTEND_SLUGS` when unset or empty.
    """
    raw = store.get_state("attend_calendars", "work") or ""
    slugs = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return slugs or DEFAULT_ATTEND_SLUGS


def _office_day_on(store: MemoryStore) -> bool:
    """Whether an in-office day is currently flagged (``office_day_until`` future).

    The ``/webhooks/departure/office-day`` toggle stores a self-expiring UTC
    timestamp; while it's in the future, work commitments use the travel-aware
    "leave now" path instead of the attend-from-here reminder.
    """
    parsed = parse_ts(store.get_state("office_day_until", "") or "")
    return parsed is not None and parsed > utcnow()


def departure_kwargs(store: MemoryStore) -> dict[str, Any]:
    """Shared :func:`plan_departure` tuning read from coaching state.

    Both the prediction (``/departure/check``) and the outcome
    (``/departure/left``) surfaces plan departures the same way, so a commitment
    is scored against the same leave-by it was nudged for.
    """
    return {
        "bias": store.get_float("time_estimation_bias", 1.0),
        "speed_kmh": store.get_float("travel_speed_kmh", DEFAULT_TRAVEL_SPEED_KMH),
        "road_factor": store.get_float("travel_road_factor", DEFAULT_ROAD_FACTOR),
        "prep_minutes": store.get_float("departure_prep_minutes", DEFAULT_PREP_MINUTES),
        "pad_fraction": store.get_float(
            TRAVEL_PAD_FRACTION_KEY, DEFAULT_TRAVEL_PAD_FRACTION
        ),
        "heads_up_minutes": store.get_float(
            "departure_heads_up_minutes", DEFAULT_HEADS_UP_MINUTES
        ),
        "soon_minutes": store.get_float("departure_soon_minutes", DEFAULT_SOON_MINUTES),
        "work_lead_minutes": store.get_float(
            "work_departure_lead_minutes", DEFAULT_WORK_LEAD_MINUTES
        ),
        "attend_slugs": _attend_slugs(store),
        "office_day": _office_day_on(store),
    }


#: Coaching-state cursor holding the last ``(commitment, level)`` a departure
#: nudge fired for, so a standing reminder debounces to once per level change.
LAST_DEPARTURE_SIGNATURE_KEY = "last_departure_signature"


@dataclass(frozen=True)
class DepartureCheck:
    """The departure-check decision (the HTTP response's model-free core).

    Attributes:
        fire: Whether a nudge should be delivered now (new ``(commitment, level)``
            since the last poll).
        message: The nudge text when ``fire`` (empty otherwise).
        reminder: The most-urgent reminder-worthy commitment's fields, or ``None``
            when nothing is due. HTTP-only decoration (dismiss link, action
            buttons) is added by the router, not here.
        location_known: Whether current coordinates were available.
    """

    fire: bool
    message: str
    reminder: dict[str, Any] | None
    location_known: bool


def plan_upcoming_departures(
    store: MemoryStore,
    *,
    current_lat: float | None = None,
    current_lon: float | None = None,
) -> list[DeparturePlan]:
    """Plan every upcoming, non-dismissed commitment — read-only, no fire/record.

    The shared plan-building body: resolve current location from the store when
    coordinates aren't supplied, read the tuning, and plan each upcoming
    commitment. Both the nudging decision (:func:`evaluate_departure_check`) and
    read-only surfaces (the widget's leave-by summary) build the same plans this
    way, so a commitment's displayed leave-by matches the one it's nudged for.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        current_lat: Current latitude, or ``None`` to read the last-known fix.
        current_lon: Current longitude, or ``None`` to read the last-known fix.

    Returns:
        A :class:`DeparturePlan` per upcoming non-dismissed commitment, in the
        store's soonest-first order.
    """
    if current_lat is None or current_lon is None:
        last = store.get_location()
        if last is not None:
            current_lat, current_lon = last["lat"], last["lon"]
    dismissed = store.dismissed_departures()
    kwargs = departure_kwargs(store)
    return [
        plan_departure(c, current_lat=current_lat, current_lon=current_lon, **kwargs)
        for c in store.upcoming_commitments()
        # Only real, own commitments earn a leave-by. An FYI event is where
        # someone else will be — you're not going anywhere for it, so it must
        # never fire a departure nudge; a placeholder/hold has nothing to leave
        # by either (see is_attendable).
        if c["id"] not in dismissed and is_attendable(c)
    ]


def evaluate_departure_check(
    store: MemoryStore,
    *,
    name: str = "",
    current_lat: float | None = None,
    current_lon: float | None = None,
) -> DepartureCheck:
    """Decide whether to nudge a departure right now, and record it if so.

    Reads current location from the store when coordinates aren't supplied, plans
    every upcoming (non-dismissed) commitment, picks the most urgent, and fires —
    debounced by a ``(commitment, level)`` signature — recording the nudge with an
    expiry at the commitment's start. Pure of HTTP concerns: the caller owns body
    parsing and any one-tap link decoration.
    """
    if current_lat is None or current_lon is None:
        last = store.get_location()
        if last is not None:
            current_lat, current_lon = last["lat"], last["lon"]

    plans = plan_upcoming_departures(
        store, current_lat=current_lat, current_lon=current_lon
    )
    top = next_departure(plans)
    location_known = current_lat is not None and current_lon is not None
    if top is None:
        return DepartureCheck(False, "", None, location_known)

    reminder = {
        "commitment_id": top.commitment["id"],
        "title": top.commitment["title"],
        "location": top.commitment.get("location"),
        "start_at": top.commitment["start_at"],
        "leave_by": top.leave_by,
        "minutes_until_leave": top.minutes_until_leave,
        "travel_minutes": top.travel_minutes,
        "basis": top.basis,
        "level": top.level,
        "mode": top.mode,
    }
    signature = f"{top.commitment['id']}:{top.level}"
    fire = signature != store.get_state(LAST_DEPARTURE_SIGNATURE_KEY, "")
    message = ""
    if fire:
        store.set_state(LAST_DEPARTURE_SIGNATURE_KEY, signature, source="inferred")
        message = build_departure_message(top, name=name)
        # Expire the nudge at the meeting's start: "leave now" is moot once it has
        # begun, so the widget won't show it for hours after.
        store.record_nudge(
            kind="departure",
            message=message,
            level=top.level,
            expires_at=top.commitment["start_at"],
        )
    return DepartureCheck(fire, message, reminder, location_known)
