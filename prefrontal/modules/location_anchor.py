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

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.coaching import CoachContext, Cue, Decision
from prefrontal.commitments import is_attendable
from prefrontal.impact import (
    cascade_at_risk,
    cascade_impact,
    cascade_phrase,
    project_free_time,
)
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.patterns import resolve_bias
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import minutes_between

#: Escalation thresholds as fractions of the stated time window.
SOFT_THRESHOLD = 0.5
FIRM_THRESHOLD = 1.0
CALL_THRESHOLD = 1.5

#: Escalation/abandon timing floors the stated window to at least this many
#: minutes. A very short window ("back in a couple minutes") would otherwise
#: collapse soft→firm→call into a burst — a 2-minute window hits 50% at ~1 min,
#: 150% at ~3 min — so the three levels can stack almost immediately. Flooring the
#: window for the *timing math* spaces them out; the percentage thresholds still
#: apply, and any window at or above this floor is unaffected.
MIN_ESCALATION_WINDOW_MINUTES = 10.0

#: Escalation levels in increasing severity. Index is the rank.
LEVELS = ("none", "soft", "firm", "call")


def _effective_window(window_minutes: float) -> float:
    """The window used for escalation/abandon timing, floored to
    :data:`MIN_ESCALATION_WINDOW_MINUTES`.

    A non-positive window is returned unchanged (there's nothing to escalate
    against). This is why a "couple minutes" outing doesn't get all three nudges
    within a few minutes — see :func:`escalation_level` / :func:`is_abandoned`.
    """
    if window_minutes <= 0:
        return window_minutes
    return max(window_minutes, MIN_ESCALATION_WINDOW_MINUTES)

#: Default radius (metres) within which the user counts as "home" — used to
#: suppress nudges and passively confirm a return when location is available.
DEFAULT_HOME_RADIUS_M = 150.0

#: Default multiple of the stated window after which an unreturned outing is
#: auto-closed as abandoned (stops it lingering active forever).
DEFAULT_ABANDON_RATIO = 3.0

#: Default grace (minutes) after location first confirms the user home before an
#: unanswered "you're home — end the outing?" prompt auto-closes it as returned.
DEFAULT_HOME_ARRIVE_GRACE_MINUTES = 10.0


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
        ``True`` if elapsed has reached ``window_minutes * ratio`` (the window
        floored to :data:`MIN_ESCALATION_WINDOW_MINUTES` for the timing, so a very
        short window doesn't auto-abandon before its escalations can even fire).
    """
    return window_minutes > 0 and elapsed_minutes >= _effective_window(window_minutes) * ratio


def escalation_level(elapsed_minutes: float, window_minutes: float) -> str:
    """Return the escalation level for an elapsed time against a window.

    Args:
        elapsed_minutes: Minutes since departure.
        window_minutes: The stated "back in N minutes" window.

    Returns:
        One of ``none``/``soft``/``firm``/``call``. ``none`` if the window is
        not positive (nothing to escalate against). The window is floored to
        :data:`MIN_ESCALATION_WINDOW_MINUTES` for the ratio, so a very short window
        can't cross all three thresholds within a few minutes.
    """
    if window_minutes <= 0:
        return "none"
    ratio = elapsed_minutes / _effective_window(window_minutes)
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


def build_home_arrival_message(intention: str, *, name: str = "") -> str:
    """Build the "you're home — end the outing?" prompt shown on arrival.

    Fired the first time location puts the user back inside the home radius (and
    re-fired each poll until they answer or the grace period auto-closes it), so
    a return is confirmed rather than assumed.

    Args:
        intention: The outing's stated mission, named in the prompt.
        name: Optional first name for a warmer greeting.

    Returns:
        The prompt string.
    """
    greeting = f"Hey {name}" if name else "Hey"
    mission = f"“{intention}”" if intention else "your outing"
    return f"{greeting}, looks like you're home — want to wrap up {mission}? Tap to end it."


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


# --- Time-window inference (when nothing is stated and nothing parses) -------
#
# A windowless intention used to be rejected with 422 — but a forgotten "back in
# N minutes" shouldn't mean no tracking at all. So we *infer* a window, mirroring
# the summarizer's "LLM with heuristic fallback" approach: ask the local model
# first, fall back to a keyword heuristic, and finally to a sane default. The
# result is always advisory — the user can still state an explicit window.

#: Window used when neither the model nor the heuristics can offer anything.
DEFAULT_INFERRED_WINDOW_MINUTES = 30.0

#: Sanity bounds for an inferred window; replies outside this are discarded so a
#: hallucinated "0" or "9999" never becomes a real escalation schedule.
MIN_INFERRED_MINUTES = 1.0
MAX_INFERRED_MINUTES = 480.0

#: Keyword → typical-minutes table for the heuristic fallback. First match wins,
#: so order from most-specific to least (e.g. "walk the dog" before "walk").
_HEURISTIC_WINDOWS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("coffee", "espresso", "latte", "cappuccino"), 15.0),
    (("walk the dog", "walk dog", "dog walk"), 20.0),
    (("gas", "fuel", "atm", "bank", "post office", "pharmacy", "drugstore"), 20.0),
    (("grocery", "groceries", "supermarket", "shopping", "the store"), 45.0),
    (("haircut", "barber", "salon"), 45.0),
    (("gym", "workout", "work out", "run", "jog", "exercise"), 60.0),
    (("lunch", "dinner", "brunch", "breakfast", "restaurant", "eat", "food"), 60.0),
    (("doctor", "dentist", "appointment", "clinic"), 60.0),
    (("walk", "errand", "errands"), 30.0),
)

#: System prompt steering the model to emit a bare integer count of minutes.
INFER_SYSTEM_PROMPT = (
    "You estimate how long a quick personal errand will take. Given a short "
    "stated intention, reply with ONLY a whole number: the minutes the person "
    "will most likely be out before returning home. No words, no units, no range."
)




def heuristic_time_window(intention: str) -> float | None:
    """Guess a window from keywords in the intention, or ``None`` if none match.

    Args:
        intention: The stated mission text.

    Returns:
        Typical minutes for the first matching keyword group, else ``None``.
    """
    lowered = intention.lower()
    for keywords, minutes in _HEURISTIC_WINDOWS:
        if any(keyword in lowered for keyword in keywords):
            return minutes
    return None


def _parse_minutes_reply(reply: str) -> float | None:
    """Extract an in-bounds minute count from a model reply, or ``None``."""
    match = re.search(r"\d+(?:\.\d+)?", reply or "")
    if not match:
        return None
    value = float(match.group())
    if value < MIN_INFERRED_MINUTES or value > MAX_INFERRED_MINUTES:
        return None
    return value


def infer_time_window(
    intention: str,
    *,
    client: Generator | None = None,
    fallback: bool = True,
) -> tuple[float, str] | None:
    """Infer a time window (minutes) for an intention with no stated window.

    Tries, in order: the local LLM (if ``client`` is given), a keyword heuristic,
    then a sane default — so any non-empty intention yields a window. Mirrors the
    summarizer's graceful degradation.

    Args:
        intention: The stated mission text.
        client: An Ollama-like client exposing ``generate``; ``None`` skips the
            model and goes straight to the heuristic.
        fallback: When ``False`` and the model is consulted but yields nothing
            usable, return ``None`` instead of degrading to heuristic/default.

    Returns:
        ``(minutes, source)`` where ``source`` is ``"llm"``/``"heuristic"``/
        ``"default"``, or ``None`` for a blank intention (or a model-only miss
        with ``fallback=False``).
    """
    if not intention or not intention.strip():
        return None

    if client is not None:
        try:
            reply = client.generate(intention.strip(), system=INFER_SYSTEM_PROMPT)
            minutes = _parse_minutes_reply(reply)
        except OllamaError:
            minutes = None
        if minutes is not None:
            return (minutes, "llm")
        if not fallback:
            return None

    heuristic = heuristic_time_window(intention)
    if heuristic is not None:
        return (heuristic, "heuristic")
    return (DEFAULT_INFERRED_WINDOW_MINUTES, "default")


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


# --- Shared per-outing decision (used by both /outing/check and the agent) ---
#
# The decision for one active outing, lifted out of the endpoint so the coaching
# agent's evaluator and the legacy /webhooks/outing/check endpoint compute it the
# same way (spec §3/§12 step 2). `evaluate_outing` is pure — it reads the outing
# dict + context and returns what to do; `apply_outing_evaluation` performs the
# store writes. Two callers, one decision, no drift.

from dataclasses import dataclass  # noqa: E402 — kept beside its use
from typing import Any  # noqa: E402

#: Escalation level → coaching urgency (spec §2). The outing ladder maps on so
#: "escalation is not optional" lives in the agent, not this module.
OUTING_URGENCY = {"soft": "nudge", "firm": "urgent", "call": "critical"}

#: Escalation level → the declared Intervention.name it realizes.
_OUTING_INTERVENTION = {"soft": "soft_nudge", "firm": "firm_nudge", "call": "voice_call"}


@dataclass(frozen=True)
class OutingEvaluation:
    """What one active outing warrants right now (pure; no side effects)."""

    action: str                 # "return" | "abandon" | "at_home" | "active"
    level: str                  # none/soft/firm/call (meaningful when active)
    fire: bool                  # a nudge is due — a new escalation level, or the arrival prompt
    message: str                # nudge text (fire only)
    impacts: list[dict[str, Any]]
    at_home: bool | None
    distance_m: float | None
    # Home-arrival tracking. arrived_at: stamp this as the outing's home_arrived_at
    # (set on first at-home detection). returned_at: backdated close time for an
    # "action == return" grace auto-close. reset_home_arrived: clear a stale arrival
    # stamp because the user left home again before ending the outing.
    arrived_at: str | None = None
    returned_at: str | None = None
    reset_home_arrived: bool = False


def evaluate_outing(
    outing: dict[str, Any],
    *,
    cur_lat: float | None,
    cur_lon: float | None,
    home_radius: float,
    abandon_ratio: float,
    bias: float,
    commitments: list[dict[str, Any]],
    name: str = "",
    lead_override: dict[int, float] | None = None,
    now: Any | None = None,
    home_grace_minutes: float = DEFAULT_HOME_ARRIVE_GRACE_MINUTES,
) -> OutingEvaluation:
    """Decide what one active outing warrants — the body of the old check loop.

    Pure: computes distance/at-home, the home-arrival state (prompt on first
    arrival, backdated auto-return once the grace period lapses), whether to
    auto-abandon, else the elapsed-time escalation level, whether a nudge is due
    (``fire``), the at-risk commitment impacts, and the message. The caller
    performs the resulting writes via :func:`apply_outing_evaluation`.

    Arriving home no longer closes the outing silently: the first at-home poll
    stamps ``home_arrived_at`` and fires a "you're home — end it?" prompt, which
    re-fires each poll until the user answers or ``home_grace_minutes`` elapses,
    at which point it auto-closes as returned **backdated to that first arrival**.

    ``lead_override`` (``{commitment_id: minutes}``) feeds real travel legs into
    the cascade instead of static ``lead_minutes`` — the caller builds it from the
    phone's location via :func:`prefrontal.departure.travel_leads`.
    """
    elapsed = outing["elapsed_minutes"] or 0.0
    window = outing["time_window_minutes"]
    now_str = (now or utcnow()).strftime(TS_FMT)
    home_arrived_at = outing.get("home_arrived_at")

    distance_m = None
    if (
        cur_lat is not None
        and cur_lon is not None
        and outing["home_lat"] is not None
        and outing["home_lon"] is not None
    ):
        distance_m = round(haversine_m(outing["home_lat"], outing["home_lon"], cur_lat, cur_lon))
    at_home = is_at_home(distance_m, home_radius) if distance_m is not None else None

    if at_home:
        if not home_arrived_at:
            # First time home this outing: remember when, and ask (don't assume).
            msg = build_home_arrival_message(outing["intention"], name=name)
            return OutingEvaluation(
                "at_home", "none", True, msg, [], at_home, distance_m, arrived_at=now_str
            )
        # Home for a while with no answer → close as returned, timed to arrival.
        home_minutes = minutes_between(home_arrived_at, now_str)
        if home_minutes is not None and home_minutes >= home_grace_minutes:
            return OutingEvaluation(
                "return", "none", False, "", [], at_home, distance_m,
                returned_at=home_arrived_at,
            )
        # Still within the grace window — re-ask.
        msg = build_home_arrival_message(outing["intention"], name=name)
        return OutingEvaluation("at_home", "none", True, msg, [], at_home, distance_m)

    # Not home (or location unknown). If we'd stamped an arrival, they left before
    # ending it — clear the stale stamp so a later real arrival re-times correctly.
    reset = bool(home_arrived_at) and at_home is False
    if is_abandoned(elapsed, window, abandon_ratio):
        return OutingEvaluation(
            "abandon", "none", False, "", [], at_home, distance_m, reset_home_arrived=reset
        )

    level = escalation_level(elapsed, window)
    fire = level_rank(level) > level_rank(outing["last_level"])
    impacts: list[dict[str, Any]] = []
    risky = []
    # Only real, own commitments topple: FYI events and placeholder/hold blocks
    # are never yours to attend as a fixed slot, so they neither consume the
    # overrun nor count as knocked-on (the same scoping the panic + endpoint
    # cascades use). ``upcoming_commitments`` doesn't pre-filter these, so do it
    # here where the chain is actually walked.
    walk = [c for c in commitments if is_attendable(c)]
    if walk:
        projected = project_free_time(outing["departure_at"], window, bias)
        # Cascade: propagate the overrun through the chain so a knocked-on
        # commitment two hops down is flagged too, not just the first collision.
        # Travel-aware when the caller supplied real per-leg leads.
        risky = cascade_at_risk(
            cascade_impact(projected, walk, lead_override=lead_override)
        )
        impacts = [
            {
                "commitment_id": i.commitment["id"],
                "title": i.commitment["title"],
                "start_at": i.commitment["start_at"],
                "projected_start": i.projected_start,
                "delay_minutes": i.delay_minutes,
                "slack_minutes": i.slack_minutes,
                "caused_by": i.caused_by,
                "hardness": i.commitment.get("hardness"),
            }
            for i in risky
        ]
    message = ""
    if fire:
        # Use the floored window so the firm message ("it's been N minutes") lines
        # up with when the level actually fires on a short, floored window.
        message = build_message(
            level,
            elapsed_minutes=elapsed,
            window_minutes=_effective_window(window),
            name=name,
        ) + cascade_phrase(risky)
    return OutingEvaluation(
        "active", level, fire, message, impacts, at_home, distance_m,
        reset_home_arrived=reset,
    )


def apply_outing_evaluation(
    store: MemoryStore, outing: dict[str, Any], ev: OutingEvaluation
) -> tuple[str, str | None]:
    """Perform the writes an :class:`OutingEvaluation` implies; return (status, outcome).

    ``return``/``abandon`` close the outing and log the episode (a ``return``
    backdates to the recorded arrival); ``at_home`` stamps the arrival and records
    the prompt nudge; an ``active`` outing that ``fire``\\s advances its one-fire
    level and records the nudge. A stale arrival stamp is cleared when the user has
    left home again. Idempotent enough that both the endpoint and the agent can
    call it.
    """
    if ev.action == "return":
        # returned_at from the eval backdates the close to first arrival; close_outing
        # would derive the same from home_arrived_at, so this is just explicit.
        closed = store.close_outing(
            outing["id"], status="returned", returned_at=ev.returned_at
        )
        return "returned", record_outing_return(store, closed)["outcome"]
    if ev.action == "abandon":
        if ev.reset_home_arrived:
            store.set_outing_home_arrived(outing["id"], None)
        closed = store.close_outing(outing["id"], status="abandoned")
        return "abandoned", record_outing_abandoned(store, closed)["outcome"]
    if ev.action == "at_home":
        if ev.arrived_at:
            store.set_outing_home_arrived(outing["id"], ev.arrived_at)
        if ev.fire:
            store.record_nudge(kind="outing", message=ev.message, level="soft")
        return "active", None
    if ev.reset_home_arrived:
        store.set_outing_home_arrived(outing["id"], None)
    if ev.fire:
        store.set_outing_level(outing["id"], ev.level)
        store.record_nudge(kind="outing", message=ev.message, level=ev.level)
    return "active", None


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
        # Grace (minutes) after arriving home before an unanswered "end it?" prompt
        # auto-closes the outing as returned (backdated to arrival).
        "home_arrive_grace_minutes": str(DEFAULT_HOME_ARRIVE_GRACE_MINUTES),
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
                description=(
                    "Suppress nudges and, on arriving home, ask whether to end the "
                    "outing — auto-closing as returned after a grace period, "
                    "backdated to first arrival."
                ),
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

    def _active_outing_evals(
        self, store: MemoryStore, ctx: CoachContext
    ) -> list[tuple[dict[str, Any], OutingEvaluation]]:
        """Evaluate every active outing — the shared, **pure** read for this tick.

        Runs the same decision (:func:`evaluate_outing`) the endpoint runs, per
        active outing, and returns ``(outing, evaluation)`` pairs. No writes: both
        :meth:`evaluate` (which turns firing evals into cues) and :meth:`after_fire`
        (which applies the implied writes) call this, so the decision is made once
        per outing and the two phases can't drift. Pinned to ``ctx.now`` so the
        re-evaluation in ``after_fire`` is identical to the one ``evaluate`` saw.
        """
        home_radius = store.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M)
        abandon_ratio = store.get_float("abandon_after_ratio", DEFAULT_ABANDON_RATIO)
        grace = store.get_float(
            "home_arrive_grace_minutes", DEFAULT_HOME_ARRIVE_GRACE_MINUTES
        )
        # Prefer the *outing*-specific bias so a coffee run projects against outing
        # history, not the task multiplier pooled with focus blocks; falls back to
        # the global (then 1.0) until there's enough outing signal.
        bias = resolve_bias(store, activity="outing")
        commitments = store.upcoming_commitments()
        # Travel-aware leads from the current location, matching the endpoint.
        # Lazy import: departure imports haversine_m from this module, so a
        # top-level import back would cycle.
        from prefrontal.departure import departure_kwargs, travel_leads

        dep = departure_kwargs(store)
        leads = travel_leads(
            commitments, ctx.current_lat, ctx.current_lon,
            bias=dep["bias"], speed_kmh=dep["speed_kmh"],
            road_factor=dep["road_factor"], prep_minutes=dep["prep_minutes"],
        )
        return [
            (
                outing,
                evaluate_outing(
                    outing,
                    cur_lat=ctx.current_lat,
                    cur_lon=ctx.current_lon,
                    home_radius=home_radius,
                    abandon_ratio=abandon_ratio,
                    bias=bias,
                    commitments=commitments,
                    name=ctx.display_name,
                    lead_override=leads,
                    now=ctx.now,
                    home_grace_minutes=grace,
                ),
            )
            for outing in store.active_outings()
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit an escalation cue for each active outing that just crossed a level.

        The coaching-agent form of ``/webhooks/outing/check``, but **read-only** —
        honoring the :meth:`~prefrontal.modules.base.Module.evaluate` contract
        (return cues, never write). It runs the shared decision
        (:func:`evaluate_outing`, via :meth:`_active_outing_evals`) per active
        outing and turns a newly-fired escalation into a
        :class:`~prefrontal.coaching.Cue` (level → urgency: soft→nudge, firm→urgent,
        call→critical). The store writes those evaluations imply — passive return,
        auto-abandon, one-fire level advance, arrival stamping — are applied in
        :meth:`after_fire`, which re-runs the identical evaluation. Passive returns
        / abandons are silent state changes and produce no cue.
        """
        cues: list[Cue] = []
        for outing, ev in self._active_outing_evals(store, ctx):
            if ev.action == "active" and ev.fire:
                cues.append(
                    Cue(
                        module=self.key,
                        intervention=_OUTING_INTERVENTION[ev.level],
                        urgency=OUTING_URGENCY[ev.level],
                        text=ev.message,
                        context_key="outing",
                        dedup_key=f"outing_escalation:{outing['id']}:{ev.level}",
                        ref={"outing_id": outing["id"]},
                    )
                )
            elif ev.action == "at_home" and ev.fire:
                # You're home — a soft "want to end it?" cue. Deduped per arrival
                # (the stamp clears when you leave), so it asks once per homecoming.
                # The first poll stamps the arrival (``ev.arrived_at``); a later
                # in-grace re-ask deliberately returns ``arrived_at=None`` (so it
                # doesn't re-stamp), but by then the arrival is persisted on the
                # outing — so key off that first, falling back to the fresh stamp,
                # so both polls share one dedup key and the prompt fires once.
                arrival_stamp = outing.get("home_arrived_at") or ev.arrived_at or "held"
                cues.append(
                    Cue(
                        module=self.key,
                        intervention="location_gating",
                        urgency="nudge",
                        text=ev.message,
                        context_key="outing",
                        dedup_key=f"outing_home_prompt:{outing['id']}:{arrival_stamp}",
                        ref={"outing_id": outing["id"]},
                    )
                )
        return cues

    def after_fire(
        self, store: MemoryStore, decisions: list[Decision], ctx: CoachContext
    ) -> None:
        """Apply the store writes each active outing's evaluation implies.

        Held back from :meth:`evaluate` so that stays a pure read (the base
        contract). Re-runs the identical per-outing decision
        (:meth:`_active_outing_evals` — deterministic, since nothing between
        collect and here mutates an outing or its commitments) and performs the
        writes via :func:`apply_outing_evaluation`: passive home-return and
        auto-abandon (both **cue-less** — they must run whether or not a nudge
        fired, which is why this can't key off ``decisions``), plus the one-fire
        level advance / arrival stamp for a firing outing. This is the same
        evaluate-then-apply the ``/webhooks/outing/check`` endpoint runs inline;
        the shared functions keep the two in parity.
        """
        for outing, ev in self._active_outing_evals(store, ctx):
            apply_outing_evaluation(store, outing, ev)

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize recent errand punctuality from closed outings."""
        # Only genuinely-returned outings have a real duration. An *abandoned*
        # outing is auto-closed with a `returned_at` stamp too (CURRENT_TIMESTAMP),
        # but the user never actually returned then — counting it would fabricate a
        # duration, the very thing `record_outing_abandoned` deliberately avoids.
        outings = [o for o in store.recent_outings(limit=100) if o.get("status") == "returned"]
        lines: list[str] = []
        if outings:
            over = 0
            for o in outings:
                # Recompute actual minutes from the stored timestamps.
                actual = minutes_between(o.get("departure_at"), o.get("returned_at"))
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


register(LocationAnchorModule())
