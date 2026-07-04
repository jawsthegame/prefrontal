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

from prefrontal.coaching import CoachContext, Cue
from prefrontal.impact import (
    cascade_at_risk,
    cascade_impact,
    cascade_phrase,
    project_free_time,
)
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import minutes_between

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

    action: str                 # "return" | "abandon" | "active"
    level: str                  # none/soft/firm/call (meaningful when active)
    fire: bool                  # a new escalation level was crossed (active only)
    message: str                # nudge text (fire only)
    impacts: list[dict[str, Any]]
    at_home: bool | None
    distance_m: float | None


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
) -> OutingEvaluation:
    """Decide what one active outing warrants — the body of the old check loop.

    Pure: computes distance/at-home, whether to passively return or auto-abandon,
    else the elapsed-time escalation level, whether it's newly crossed (``fire``),
    the at-risk commitment impacts, and the nudge message. The caller performs the
    resulting writes via :func:`apply_outing_evaluation`.
    """
    elapsed = outing["elapsed_minutes"] or 0.0
    window = outing["time_window_minutes"]

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
        return OutingEvaluation("return", "none", False, "", [], at_home, distance_m)
    if is_abandoned(elapsed, window, abandon_ratio):
        return OutingEvaluation("abandon", "none", False, "", [], at_home, distance_m)

    level = escalation_level(elapsed, window)
    fire = level_rank(level) > level_rank(outing["last_level"])
    impacts: list[dict[str, Any]] = []
    risky = []
    if commitments:
        projected = project_free_time(outing["departure_at"], window, bias)
        # Cascade: propagate the overrun through the chain so a knocked-on
        # commitment two hops down is flagged too, not just the first collision.
        risky = cascade_at_risk(cascade_impact(projected, commitments))
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
        message = build_message(
            level, elapsed_minutes=elapsed, window_minutes=window, name=name
        ) + cascade_phrase(risky)
    return OutingEvaluation("active", level, fire, message, impacts, at_home, distance_m)


def apply_outing_evaluation(
    store: MemoryStore, outing: dict[str, Any], ev: OutingEvaluation
) -> tuple[str, str | None]:
    """Perform the writes an :class:`OutingEvaluation` implies; return (status, outcome).

    ``return``/``abandon`` close the outing and log the episode; an ``active``
    outing that ``fire``\\s advances its one-fire level and records the nudge.
    Idempotent enough that both the endpoint and the agent can call it.
    """
    if ev.action == "return":
        closed = store.close_outing(outing["id"], status="returned")
        return "returned", record_outing_return(store, closed)["outcome"]
    if ev.action == "abandon":
        closed = store.close_outing(outing["id"], status="abandoned")
        return "abandoned", record_outing_abandoned(store, closed)["outcome"]
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

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit an escalation cue for each active outing that just crossed a level.

        The coaching-agent form of ``/webhooks/outing/check``: it runs the same
        shared decision (:func:`evaluate_outing`) and side effects
        (:func:`apply_outing_evaluation` — passive return, auto-abandon, one-fire
        level advance) per active outing, then turns a newly-fired escalation into
        a :class:`~prefrontal.coaching.Cue` (level → urgency: soft→nudge,
        firm→urgent, call→critical). Passive returns / abandons are silent state
        changes and produce no cue.
        """
        home_radius = store.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M)
        abandon_ratio = store.get_float("abandon_after_ratio", DEFAULT_ABANDON_RATIO)
        bias = store.get_float("time_estimation_bias", 1.0)
        commitments = store.upcoming_commitments()
        cues: list[Cue] = []
        for outing in store.active_outings():
            ev = evaluate_outing(
                outing,
                cur_lat=ctx.current_lat,
                cur_lon=ctx.current_lon,
                home_radius=home_radius,
                abandon_ratio=abandon_ratio,
                bias=bias,
                commitments=commitments,
                name=ctx.display_name,
            )
            apply_outing_evaluation(store, outing, ev)
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
        return cues

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize recent errand punctuality from closed outings."""
        outings = [o for o in store.recent_outings(limit=100) if o.get("returned_at")]
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
