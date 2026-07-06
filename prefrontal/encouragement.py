"""Encouragement & recovery layer — when a day is going badly, shift tone.

The counterweight to a system whose whole job is nudging: once a day crosses a
"rough" threshold, stop nudging and *reassure* — acknowledge the rough day
without judgment, then hand back a concrete get-back-on-track plan (re-fit what
still fits, suggest a low-stakes deferral, and one tiny next step). Specced in
``docs/encouragement.md``; this is the standalone core (the coaching agent can
later wrap :func:`assess_day` as one more cue producer — one implementation).

Two layers, like the briefing and the panic triage:

- deterministic and testable — :func:`assess_day` scores today's signals and
  decides ``rough``; :func:`build_recovery` turns that into recommendations;
  :func:`render_encouragement` writes tone-calibrated Markdown;
- optional — :func:`summarize_encouragement` rewrites it as warmer prose via
  Ollama, falling back to the deterministic text.

Everything it reads is already computed (miss episodes, conflicts, avoided
todos, free windows, drift, and — reusing panic mode's
:func:`~prefrontal.panic.overwhelm_level` — whether the plate is *overwhelmed
right now*). It's **off by default** (the ``encouragement`` coaching key) and
never invents data — the ``signals`` list is auditable, so a user can see
exactly *why* a day was flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from prefrontal.briefing import DEFAULT_DAY_END_HOUR, DEFAULT_DAY_START_HOUR
from prefrontal.clock import TS_FMT
from prefrontal.clock import parse_ts as _parse_or_none
from prefrontal.commitments import find_conflicts
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
from prefrontal.memory.patterns import task_bias_resolver
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import (
    free_windows,
    local_datetime,
    local_day_bounds,
    local_time_utc,
    suggest_for_windows,
    window_config_for,
)
from prefrontal.todos import avoided_todos, decompose_task

if TYPE_CHECKING:
    from prefrontal.briefing import Briefing
    from prefrontal.integrations.ollama import OllamaClient

#: ``rough_score`` at/above which a day is flagged (the ``encouragement_threshold``
#: coaching key overrides it). One missed hard commitment (3.0) trips it alone;
#: so do three smaller misses. A single stray late reply (1.0) does not.
DEFAULT_ROUGH_THRESHOLD = 3.0

#: Signal weights (spec §3.1). Abandoned/overrun outings already log a ``miss``
#: episode, so they score once as ``miss_episode`` rather than double-counting.
#: ``overwhelmed`` is the acute "buried right now" signal lifted from panic mode
#: (:func:`~prefrontal.panic.overwhelm_level`); weighted like a missed hard
#: commitment so a genuinely overwhelmed plate trips a rough day on its own —
#: reusing panic's tuned pressing thresholds instead of a second one of our own.
SIGNAL_WEIGHTS = {
    "missed_hard": 3.0,
    "miss_episode": 1.0,
    "conflict": 0.5,
    "overwhelmed": 3.0,
}

#: A drift pattern at/above this observed value (with real confidence) adds a
#: small modifier that can tip a borderline day over — but never trips it alone.
DRIFT_BASELINE = 0.5
DRIFT_MIN_CONFIDENCE = 0.3
DRIFT_MODIFIER = 0.5

#: Ceiling for the recovery plan's "one small step" (minutes).
FIRST_STEP_MAX_MINUTES = 5.0

#: Morning-briefing note thresholds (spec §6.2). Deliberately simple and tunable.
#: A day reads as *packed* at this many commitments, or this many *hard* ones.
PACKED_COMMITMENTS = 6
PACKED_HARD = 3
#: A *recent* rough stretch: this many ``miss`` episodes over the briefing's 7-day
#: window, or this many todos being actively avoided.
RECENT_MISS_THRESHOLD = 3
RECENT_AVOIDED_THRESHOLD = 3
#: A *wide-open* day: at most this many commitments, none of them hard.
OPEN_MAX_COMMITMENTS = 1

#: Coaching-state key holding the user's standing answer to the open-day choice
#: (§6.2): ``relax`` (open days are rest days) or ``accomplish`` (shape a light
#: plan). Absent/other ⇒ the briefing presents the choice instead of acting on it.
OPEN_DAY_KEY = "open_day_choice"
OPEN_DAY_CHOICES = ("relax", "accomplish")

ENCOURAGEMENT_SYSTEM_PROMPTS = {
    "warm": (
        "You are Prefrontal's recovery voice for someone with ADHD whose day has "
        "gone sideways. Rewrite the assessment + plan as a short, warm, "
        "second-person message: acknowledge the rough day without judgment or "
        "cheerleading, then give the single first step verbatim and mention what "
        "still fits. Do not invent events or add tasks. Do not minimize, but do "
        "not catastrophize. A few sentences. No preamble."
    ),
    "plain": (
        "You are Prefrontal's recovery voice, matter-of-fact register. Restate "
        "the assessment + plan plainly and briefly for someone who wants the "
        "recovery content without softness: name that the day ran rough, give the "
        "single first step verbatim, and what still fits. No affect words, no "
        "cheerleading, no invented events. A few sentences. No preamble."
    ),
}


# --- Timestamp helpers -------------------------------------------------------


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _commitment_title(context: str | None) -> str | None:
    """Recover a commitment title from a departure episode's context, or ``None``.

    Departure misses log ``context="commitment: X"`` (one-tap "missed it") or
    ``"auto departure: X"`` (geofence capture).
    """
    if not context:
        return None
    for prefix in ("commitment:", "auto departure:"):
        if context.startswith(prefix):
            return context[len(prefix):].strip() or None
    return None


# --- Assessment --------------------------------------------------------------


@dataclass(frozen=True)
class DayAssessment:
    """How today is going, and why (deterministic)."""

    date: str
    rough: bool
    rough_score: float
    signals: list[dict[str, Any]]  # each {kind, detail, weight}
    enabled: bool                  # the encouragement master switch
    tone: str = "warm"


def _drift_above_baseline(store: MemoryStore) -> bool:
    for p in store.get_patterns("drift"):
        if (p.get("observed_value") or 0.0) >= DRIFT_BASELINE and (
            p.get("confidence") or 0.0
        ) >= DRIFT_MIN_CONFIDENCE:
            return True
    return False


def assess_day(store: MemoryStore, now: Any | None = None) -> DayAssessment:
    """Score today's signals and decide whether it's a rough day (spec §3).

    Reads today-scoped signals: ``miss`` episodes over ``[midnight, now]``, and
    today's commitments over the full day (for hard-miss classification and
    unresolved double-bookings), plus a small rising-drift modifier. It also folds
    in whether the plate is *overwhelmed right now* — reusing panic mode's
    :func:`~prefrontal.panic.overwhelm_level` over the current pressure buckets, so
    a day that's buried (several things bearing down) reads rough even before
    anything has been *missed*. A missed *hard* commitment — or an overwhelmed
    plate — is heaviest. Inert (``rough=False``) unless the ``encouragement``
    coaching key is ``on``.
    """
    now = now or utcnow()
    # Today's signals are scoped to the user's *local* day (so an evening check
    # after the UTC rollover still assesses today, not tomorrow).
    tz = get_settings().timezone
    day_start, _day_end = local_day_bounds(now, tz)
    date = local_datetime(now, tz).strftime("%Y-%m-%d")
    enabled = (store.get_state("encouragement", "off") or "off").lower() == "on"
    tone = (store.get_state("encouragement_tone", "warm") or "warm").lower()
    tone = tone if tone in ENCOURAGEMENT_SYSTEM_PROMPTS else "warm"
    if not enabled:
        return DayAssessment(date, False, 0.0, [], enabled=False, tone=tone)

    threshold = store.get_float("encouragement_threshold", DEFAULT_ROUGH_THRESHOLD)
    # Scan the *whole* day for commitments (not just up to `now`): a hard
    # commitment you already logged a miss for should score as missed_hard
    # regardless of whether its clock time is earlier or later than the moment
    # this assessment runs — otherwise a morning check misclassifies an all-day
    # hard miss as a generic miss. Miss *episodes* below stay [midnight, now].
    today = store.commitments_between(_fmt(day_start), _fmt(_day_end))
    hard_titles = {
        (c.get("title") or "").lower()
        for c in today
        if c.get("hardness") == "hard"
    }

    signals: list[dict[str, Any]] = []
    for e in store.episodes_since(_fmt(day_start)):
        if e.get("outcome") != "miss":
            continue
        title = _commitment_title(e.get("context"))
        if (
            e.get("episode_type") == "departure"
            and title
            and title.lower() in hard_titles
        ):
            signals.append(
                {"kind": "missed_hard", "detail": f"'{title}' — missed", "weight": 3.0}
            )
        else:
            detail = e.get("context") or e.get("episode_type") or "a miss"
            signals.append({"kind": "miss_episode", "detail": detail, "weight": 1.0})

    for c in find_conflicts(today):
        signals.append(
            {
                "kind": "conflict",
                "detail": f"{c.a['title']} ↔ {c.b['title']}",
                "weight": 0.5,
            }
        )

    # Acute overwhelm: reuse panic mode's overwhelm classifier (its own tuned
    # pressing thresholds) rather than inventing a second one. A plate that's
    # buried *right now* — several things bearing down, something already late —
    # is a rough day even if nothing has been missed yet.
    from prefrontal.panic import (
        DEFAULT_ALERT_MIN_PRESSING,
        build_panic,
        overwhelm_level,
    )

    try:
        min_pressing = int(
            store.get_state("panic_alert_min_pressing") or DEFAULT_ALERT_MIN_PRESSING
        )
    except (TypeError, ValueError):
        min_pressing = DEFAULT_ALERT_MIN_PRESSING
    plan = build_panic(store, now=now)
    if overwhelm_level(plan, min_pressing=min_pressing) == "overwhelmed":
        late = plan.counts.get("late", 0)
        pressing = plan.counts.get("pressing", 0)
        signals.append(
            {
                "kind": "overwhelmed",
                "detail": f"{late} already late, {pressing} bearing down right now",
                "weight": SIGNAL_WEIGHTS["overwhelmed"],
            }
        )

    score = sum(s["weight"] for s in signals)
    if _drift_above_baseline(store):
        score += DRIFT_MODIFIER
    rough_score = round(score, 1)
    return DayAssessment(
        date, rough_score >= threshold, rough_score, signals, enabled=True, tone=tone
    )


# --- Recovery plan -----------------------------------------------------------


@dataclass(frozen=True)
class RecoveryPlan:
    """Concrete get-back-on-track recommendations for a rough day."""

    refit: list[dict[str, Any]]      # {start, minutes, suggestion, todo_id}
    defer: list[dict[str, Any]]      # {title, commitment_id, reason}
    first_step: dict[str, Any] | None  # {todo_id, title, step, minutes}


_EMPTY_PLAN = RecoveryPlan(refit=[], defer=[], first_step=None)


def build_recovery(
    store: MemoryStore, assessment: DayAssessment, now: Any | None = None
) -> RecoveryPlan:
    """Turn a rough assessment into recommendations (spec §4); empty if not rough.

    Composes only existing logic: the briefing's free-window re-fit
    (:func:`suggest_for_windows`), a soft-commitment deferral suggestion (hard
    ones are never suggested), and one tiny first step via
    :func:`~prefrontal.todos.decompose_task` over the most-avoided todo.
    """
    if not assessment.rough:
        return _EMPTY_PLAN
    now = now or utcnow()
    settings = get_settings()
    tz = settings.timezone
    # Local day + local available-hours band (see build_briefing): a UTC-anchored
    # band would re-fit todos into the wrong hours for a non-UTC user.
    day_start, day_end = local_day_bounds(now, tz)
    todos = store.open_todos()

    # 1. Re-fit the rest of the day (now → the day's available-hours end).
    refit: list[dict[str, Any]] = []
    today = store.commitments_between(_fmt(day_start), _fmt(day_end))
    band_start = max(now, local_time_utc(now, tz, DEFAULT_DAY_START_HOUR))
    band_end = local_time_utc(now, tz, DEFAULT_DAY_END_HOUR)
    if todos and band_end > band_start:
        bias = store.get_float("time_estimation_bias", 1.0)
        window_config = window_config_for(settings, store)
        # Each remaining window is re-fit with the bias for its own time of day,
        # and each candidate by its own energy/category (§5).
        for s in suggest_for_windows(
            free_windows(today, band_start, band_end),
            todos,
            bias,
            config=window_config,
            tz=settings.timezone,
            resolver_for_hour=lambda hour: task_bias_resolver(store, local_hour=hour),
        ):
            pick = s["suggestion"]
            if pick is not None:
                refit.append(
                    {
                        "start": s["window"].start,
                        "minutes": s["window"].minutes,
                        "suggestion": pick["title"],
                        "todo_id": pick["id"],
                    }
                )

    # 2. Suggest deferring the lowest-stakes commitments still ahead (never hard).
    defer = [
        {
            "title": c.get("title"),
            "commitment_id": c.get("id"),
            "reason": "soft — safe to move if today's full",
        }
        for c in today
        if c.get("hardness") != "hard"
        and c.get("kind") != "fyi"
        and (_parse_or_none(c.get("start_at")) or now) >= now
    ]

    # 3. One small next step: shrink the most-avoided todo until it's unintimidating.
    first_step = None
    avoided = avoided_todos(todos, now)
    if avoided:
        top = avoided[0]["todo"]
        decomp = decompose_task(top["title"], max_first_minutes=FIRST_STEP_MAX_MINUTES)
        first_step = {
            "todo_id": top["id"],
            "title": top["title"],
            "step": decomp.first_step,
            "minutes": decomp.first_step_minutes,
        }
    elif refit:
        pick = refit[0]
        decomp = decompose_task(pick["suggestion"], max_first_minutes=FIRST_STEP_MAX_MINUTES)
        first_step = {
            "todo_id": pick["todo_id"],
            "title": pick["suggestion"],
            "step": decomp.first_step,
            "minutes": decomp.first_step_minutes,
        }
    return RecoveryPlan(refit=refit, defer=defer, first_step=first_step)


# --- Rendering ---------------------------------------------------------------


def render_encouragement(
    assessment: DayAssessment, plan: RecoveryPlan, *, tz: str | None = None
) -> str:
    """Render the assessment + plan as tone-calibrated Markdown (deterministic).

    ``warm`` acknowledges the rough day; ``plain`` states it matter-of-factly.
    Empty string when the day isn't rough (nothing to say).
    """
    if not assessment.rough:
        return ""
    tz = tz or get_settings().timezone
    warm = assessment.tone != "plain"
    lines: list[str] = []
    if warm:
        lines.append(
            "**Today got away from you — that happens, and it's not a referendum "
            "on you.** Here's a smaller shape for the rest of it."
        )
    else:
        lines.append("**Today ran rough.** Here's what still fits and where to restart.")
    lines.append("")

    if plan.first_step:
        fs = plan.first_step
        lead = "If you only do one thing" if warm else "Restart with"
        lines.append(f"**{lead}:** {fs['step']}")
        lines.append(f"_(gets “{fs['title']}” moving — about {round(fs['minutes'])} min)_")
        lines.append("")

    fits = [r for r in plan.refit if r.get("suggestion")]
    if fits:
        lines.append("**What still fits today:**")
        lines.extend(
            f"- {_hhmm(r['start'], tz)} (~{round(r['minutes'])}m) — {r['suggestion']}"
            for r in fits
        )
        lines.append("")

    if plan.defer:
        lines.append("**Safe to move if you need the room:**")
        lines.extend(f"- {d['title']}" for d in plan.defer)
        lines.append("")

    lines.append(
        "_One thing, then reassess. A rough day isn't a rough week._" if warm
        else "_Do the first step, then reassess._"
    )
    return "\n".join(lines).rstrip() + "\n"


def _hhmm(ts: str, tz: str) -> str:
    """Render a stored naive-UTC timestamp as local ``HH:MM`` in ``tz``.

    Window starts are naive UTC; slicing ``[11:16]`` off the string would print
    the UTC wall clock (hours ahead for a non-UTC user). Convert first, the same
    way the briefing renders its times.
    """
    dt = _parse_or_none(ts)
    return local_datetime(dt, tz).strftime("%H:%M") if dt else str(ts)


# --- Optional prose pass -----------------------------------------------------


@dataclass(frozen=True)
class EncouragementResult:
    """Result of :func:`summarize_encouragement`."""

    text: str
    source: str  # "llm" | "heuristic"
    rough: bool
    model: str | None = None


def summarize_encouragement(
    store: MemoryStore,
    *,
    client: OllamaClient | None = None,
    now: Any | None = None,
    fallback: bool = True,
) -> EncouragementResult:
    """Assess the day and, if rough, rewrite the recovery message as warm prose.

    Returns the deterministic render on a not-rough day, an empty text with
    ``rough=False`` when nothing's wrong, or Ollama prose (heuristic fallback) for
    a rough day — mirroring :func:`prefrontal.briefing.summarize_briefing`.
    """
    from prefrontal.integrations.ollama import OllamaClient, OllamaError

    now = now or utcnow()
    assessment = assess_day(store, now=now)
    plan = build_recovery(store, assessment, now=now)
    rendered = render_encouragement(assessment, plan)
    if not assessment.rough:
        return EncouragementResult(text=rendered, source="heuristic", rough=False)

    client = client or OllamaClient.from_settings()
    system = ENCOURAGEMENT_SYSTEM_PROMPTS.get(
        assessment.tone, ENCOURAGEMENT_SYSTEM_PROMPTS["warm"]
    )
    try:
        prose = client.generate(rendered, system=system).strip()
    except OllamaError:
        if not fallback:
            raise
        return EncouragementResult(text=rendered, source="heuristic", rough=True)
    if not prose:
        if not fallback:
            raise OllamaError("Ollama returned an empty encouragement message.")
        return EncouragementResult(text=rendered, source="heuristic", rough=True)
    return EncouragementResult(text=prose, source="llm", rough=True, model=client.model)


# --- Morning-briefing note (spec §6.2) ---------------------------------------
#
# The briefing's closing line, tone-shifted by how the day (and the recent
# stretch) is shaping up. This is the "briefing variant" of the layer: instead of
# a separate recovery message, the *morning* brief itself gets a sentence or two —
# encouragement on a packed/rough day, and a relax-vs-accomplish choice on a
# wide-open one. Deterministic and gated on the same ``encouragement`` opt-in as
# the rest of the layer, so it stays inert (and the brief keeps its usual time-bias
# reminder) unless the user turns it on.


def _n_things(n: int, noun: str) -> str:
    """Pluralize a small count: ``_n_things(1, "task") == "1 task"``."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _open_optional_item(briefing: Briefing) -> str | None:
    """The single lowest-stakes thing to offer on a rest day (or ``None``)."""
    if briefing.avoided:
        return briefing.avoided[0]["title"]
    for s in briefing.spare:
        if s.get("suggestion"):
            return s["suggestion"]
    return None


def _open_relax_note(briefing: Briefing, warm: bool) -> str:
    item = _open_optional_item(briefing)
    if warm:
        base = (
            "Today's wide open, and you've set open days as rest days — so no agenda "
            "from me."
        )
        if item:
            return (
                base + f" If you want one tiny, optional win, “{item}” would fit "
                "without turning today into work."
            )
        return base + " Genuinely nothing pressing — take the day."
    base = "Open day; set to rest. No agenda."
    return base + (f" Optional single item: “{item}”." if item else "")


def _open_accomplish_note(briefing: Briefing, warm: bool) -> str:
    picks = [s for s in briefing.spare if s.get("suggestion")][:2]
    if picks:
        shape = ", ".join(f"{_hhmm(s['start'], briefing.tz)} {s['suggestion']}" for s in picks)
        if warm:
            return (
                "Today's wide open and you're in make-it-count mode — here's a light "
                f"shape: {shape}. Start with the smallest and let it build."
            )
        return f"Open day, make-it-count mode. Shape: {shape}. Start with the smallest."
    if briefing.avoided:
        item = briefing.avoided[0]["title"]
        if warm:
            return (
                "Today's wide open and you're in make-it-count mode. Nothing auto-fit "
                f"a window, but “{item}” is the one worth a first swing."
            )
        return f"Open day, make-it-count mode. Best target: “{item}”."
    if warm:
        return (
            "Today's wide open and you're in make-it-count mode — and your list is "
            "clear too. Nothing pressing, so make it count however you like."
        )
    return "Open day, make-it-count mode. List is clear — nothing pressing to assign."


def _open_note(store: MemoryStore, briefing: Briefing, warm: bool) -> str:
    """Present the relax-vs-accomplish choice, or act on the standing answer."""
    choice = (store.get_state(OPEN_DAY_KEY, "") or "").strip().lower()
    if choice not in OPEN_DAY_CHOICES:
        if warm:
            return (
                "Today's wide open — nothing hard on the calendar. Do you want to take "
                "it easy or make it count? Tell me relax or accomplish and I'll shape "
                "the day to match."
            )
        return (
            "Open day, nothing hard scheduled. Rest, or make it count? Set relax or "
            "accomplish and the brief will match."
        )
    if choice == "relax":
        return _open_relax_note(briefing, warm)
    return _open_accomplish_note(briefing, warm)


def briefing_note(
    store: MemoryStore, briefing: Briefing, now: Any | None = None
) -> str | None:
    """The morning brief's closing encouragement line, or ``None`` (spec §6.2).

    Reads the shape of the day off an already-built :class:`~prefrontal.briefing.Briefing`
    (plus today's acute :func:`assess_day` and the recent 7-day slips it already
    carries) and returns at most a sentence or two:

    - **today already rough** (e.g. an overnight missed hard commitment) — a
      no-judgment "one thing at a time, you've got this";
    - **a rough recent stretch** (misses piling up / things being avoided) —
      encouragement to go gentle and space things out rather than catch up at once;
    - **a packed day** (many commitments, or several hard) — "you've got this;
      leave breathing room";
    - **a wide-open day** — presents the relax-vs-accomplish choice, or, once the
      user has answered (:data:`OPEN_DAY_KEY`), a rest note or a light plan.

    Returns ``None`` on an ordinary day, or whenever the ``encouragement`` layer is
    off — so the briefing falls back to its usual time-bias reminder. Never invents
    events: every branch is driven by counts already in ``briefing``.
    """
    now = now or utcnow()
    assessment = assess_day(store, now=now)
    if not assessment.enabled:
        return None
    warm = assessment.tone != "plain"

    today = briefing.today
    n = len(today)
    hard = sum(1 for c in today if c.get("hardness") == "hard")
    recent_misses = sum(briefing.slips.values())
    avoided_n = len(briefing.avoided)

    # 1. Today already went sideways (acute, today-scoped).
    if assessment.rough:
        if warm:
            return (
                "Rough start already — that's not a verdict on you. Take today one "
                "thing at a time and let the rest wait; you've got this."
            )
        return "Rough start. One thing at a time; let non-essentials wait."

    # 2. A rough recent stretch — the 7-day misses / avoidance the brief carries.
    if recent_misses >= RECENT_MISS_THRESHOLD or avoided_n >= RECENT_AVOIDED_THRESHOLD:
        bits: list[str] = []
        if recent_misses:
            bits.append(_n_things(recent_misses, "thing") + " slipped")
        if avoided_n:
            bits.append(_n_things(avoided_n, "task") + " you keep putting off")
        detail = " and ".join(bits)
        if warm:
            return (
                f"The last stretch has been heavy — {detail}. You've got this: go "
                "gentle today and space things out rather than trying to catch up all "
                "at once."
            )
        return (
            f"Heavy stretch recently ({detail}). Keep today light and spaced; don't "
            "try to catch up all at once."
        )

    # 3. A packed day.
    if n >= PACKED_COMMITMENTS or hard >= PACKED_HARD:
        if hard == 0:
            hard_bit = ""
        elif hard == n:
            hard_bit = ", all hard"
        else:
            hard_bit = f", {hard} hard"
        if warm:
            return (
                f"Packed day — {_n_things(n, 'commitment')}{hard_bit}. You've got "
                "this: leave a little breathing room between things and try not to "
                "pack the gaps too tight."
            )
        return (
            f"Full day: {_n_things(n, 'commitment')}{hard_bit}. Leave buffers between "
            "them and keep the gaps loose."
        )

    # 4. A wide-open day — offer the choice, or act on the standing answer.
    if n <= OPEN_MAX_COMMITMENTS and hard == 0:
        return _open_note(store, briefing, warm)

    # Otherwise: an ordinary day — let the briefing keep its time-bias reminder.
    return None


# --- Debounce cursor ---------------------------------------------------------

#: Coaching-state key holding the last UTC date (YYYY-MM-DD) an encouragement was
#: delivered — caps delivery at once per day regardless of poll frequency (§5).
_SENT_KEY = "last_encouragement_date"


def already_sent_today(store: MemoryStore, now: Any | None = None) -> bool:
    """Whether an encouragement was already delivered today (debounce read)."""
    now = now or utcnow()
    return store.get_state(_SENT_KEY, "") == now.strftime("%Y-%m-%d")


def mark_sent_today(store: MemoryStore, now: Any | None = None) -> None:
    """Stamp today as delivered so a later poll won't re-send (explicit write)."""
    now = now or utcnow()
    store.set_state(_SENT_KEY, now.strftime("%Y-%m-%d"), source="inferred")
