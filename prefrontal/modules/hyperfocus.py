"""Hyperfocus module.

Hyperfocus is sustained, intense concentration. It is double-edged: *productive*
hyperfocus on the thing that matters is a superpower; *misdirected* hyperfocus
(on the wrong task, or past the point of eating/sleeping/leaving) is costly. The
distinction is the whole point of this module — it should protect good
hyperfocus and gently interrupt bad hyperfocus, never blanket-interrupt both.

Its signature in the memory layer is long ``task`` blocks where ``actual_value``
(duration) greatly exceeds ``predicted_value``, combined with whether the block
was on an intended task (``context``) and whether nudges were ``acknowledged``.

This module is driven by an explicit **focus session** (the ``focus_sessions``
table): the user declares "getting into X" and, optionally, how long they mean
to spend. The asymmetry lives in the pure helpers below — :func:`focus_level`
decides *whether* to interrupt and :func:`should_protect` decides whether to
*suppress* other modules' non-critical nudges while an aligned block is healthy:

- **none** — below the soft point: protected if aligned, nothing fires.
- **check** — past the planned duration (or the soft-block default if none was
  stated): one gentle alignment check. An aligned block is still protected.
- **break** — past the hard ceiling: a biological break fires regardless of
  alignment, and protection lifts.

The webhook layer (``/webhooks/focus/{start,check,end}``) and an n8n poller wire
these into real nudges, mirroring the Location-Aware Task Anchor escalation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from prefrontal.clock import TS_FMT, local_datetime, local_day_bounds
from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.coaching import CoachContext, Cue
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import minutes_between

#: Interrupt levels in ascending severity. ``none`` is rank 0; an interrupt fires
#: only when a *new* (higher-ranked) level is crossed since the last poll.
FOCUS_LEVELS = ("none", "check", "break")

#: Default coaching-state values (minutes / ratio) when none is configured.
DEFAULT_SOFT_BLOCK_MINUTES = 90.0
DEFAULT_HARD_INTERRUPT_MINUTES = 180.0
#: Auto-close a session left open past this multiple of the hard ceiling — a
#: forgotten "surfacing" tap should not linger active forever.
DEFAULT_FOCUS_ABANDON_RATIO = 2.0

#: Maps the one-tap exit rating to the resulting ``episodes.outcome`` value.
FOCUS_OUTCOME: dict[str, str] = {
    "worth_it": "success",
    "should_have_stopped": "partial",
    "pulled_off": "miss",
}


def level_rank(level: str) -> int:
    """Return the severity rank of an interrupt level (``none`` -> 0)."""
    return FOCUS_LEVELS.index(level)


def focus_level(
    elapsed_minutes: float,
    *,
    planned_minutes: float | None,
    soft_block_minutes: float = DEFAULT_SOFT_BLOCK_MINUTES,
    hard_interrupt_minutes: float = DEFAULT_HARD_INTERRUPT_MINUTES,
) -> str:
    """Return the interrupt level for an elapsed focus block.

    The hard ceiling dominates (a biological break is alignment-independent). The
    soft check fires once the block overruns its *planned* duration — or, when no
    duration was stated, the soft-block default. Protecting within the planned
    window is the point: a declared four-hour deep-work block should not get a
    90-minute interrupt.

    Args:
        elapsed_minutes: Minutes since the session started.
        planned_minutes: The stated intended duration, or ``None`` if none given.
        soft_block_minutes: Fallback soft-check point when no duration was stated.
        hard_interrupt_minutes: The biological-break ceiling.

    Returns:
        One of ``none``/``check``/``break``.
    """
    if hard_interrupt_minutes > 0 and elapsed_minutes >= hard_interrupt_minutes:
        return "break"
    soft_point = (
        planned_minutes if planned_minutes and planned_minutes > 0 else soft_block_minutes
    )
    if soft_point > 0 and elapsed_minutes >= soft_point:
        return "check"
    return "none"


def should_protect(level: str, *, aligned: bool, protect_enabled: bool = True) -> bool:
    """Whether to suppress other modules' non-critical nudges right now.

    "Protect" is the heart of the module: while an aligned block is healthy
    (below the hard ceiling), the system should *raise the bar* for what reaches
    the user. Only a ``break`` lifts protection; a ``check`` still protects (it is
    itself the one gentle interrupt allowed during an overrun).

    Args:
        level: The current :func:`focus_level`.
        aligned: Whether the session is on the intended task (the protect bit).
        protect_enabled: The ``protect_aligned_hyperfocus`` preference.

    Returns:
        ``True`` when aligned hyperfocus should shield the user from noise.
    """
    return protect_enabled and aligned and level != "break"


def is_abandoned(
    elapsed_minutes: float,
    hard_interrupt_minutes: float = DEFAULT_HARD_INTERRUPT_MINUTES,
    ratio: float = DEFAULT_FOCUS_ABANDON_RATIO,
) -> bool:
    """Whether a session has run long enough to be auto-closed as abandoned.

    Args:
        elapsed_minutes: Minutes since the session started.
        hard_interrupt_minutes: The biological-break ceiling.
        ratio: Multiple of the ceiling beyond which an open session is presumed
            forgotten rather than genuinely still running.

    Returns:
        ``True`` if elapsed has reached ``hard_interrupt_minutes * ratio``.
    """
    return hard_interrupt_minutes > 0 and elapsed_minutes >= hard_interrupt_minutes * ratio


def build_focus_message(
    level: str,
    *,
    task: str,
    elapsed_minutes: float,
    aligned: bool = True,
    name: str = "",
) -> str:
    """Build the user-facing message for an interrupt level.

    Args:
        level: ``check`` or ``break``.
        task: The intended task, quoted back so the nudge is concrete.
        elapsed_minutes: Minutes since the session started (rendered whole).
        aligned: Whether the block is on the intended task — a ``check`` on a
            misaligned block reads as a redirect rather than a "still good?".
        name: Optional first name; included when set.

    Returns:
        The message string. Empty for ``none``/unknown levels.
    """
    elapsed = round(elapsed_minutes)
    greeting = f"Hey {name}" if name else "Hey"
    if level == "check":
        if aligned:
            return (
                f"{greeting}, {elapsed} min on '{task}'. Still what you meant to "
                "be doing? Keep going, or tap Wrap up to end the session."
            )
        return (
            f"{greeting}, you've spent {elapsed} min and you meant to be on "
            f"'{task}'. Worth staying on this, or park it for later?"
        )
    if level == "break":
        return (
            f"{greeting}, {elapsed} minutes deep on '{task}'. Stand up, water, "
            "eyes off the screen for a minute — then dive back in."
        )
    return ""


def _todo_tags(store: MemoryStore, closed: dict) -> tuple[str | None, str | None]:
    """The ``(energy, category)`` of the todo a focus block was working, or ``(None, None)``.

    A session started with a ``todo_id`` carries the task's energy load and
    category onto its close episode, so the time-estimation bias can condition on
    them (learning §5). A session with no link (free-text ``intended_task``) or a
    since-deleted todo simply contributes untagged, exactly as before.
    """
    todo_id = closed.get("todo_id")
    if not todo_id:
        return (None, None)
    todo = store.get_todo(todo_id)
    if not todo:
        return (None, None)
    return (todo.get("energy"), todo.get("category"))


# --- Zero-field ("one tap") start --------------------------------------------
#
# Declaring a focus session — a task + a duration — is friction at exactly the
# wrong moment: right when you're trying to drop in. The whole product premise is
# that you shouldn't have to remember to reach for it. So a start with no fields
# infers what you'd have typed: the task you *should* be getting into (your
# most-avoided / highest-priority open todo, carrying its estimate as the planned
# duration), falling back to a generic block so the one-tap button always works.

#: Task label when there's nothing open to infer a focus session from.
DEFAULT_FOCUS_TASK = "a focus block"


@dataclass(frozen=True)
class InferredFocus:
    """A focus start inferred for a zero-field (one-tap) session.

    Attributes:
        intended_task: The task to protect — an open todo's title, or a generic
            fallback.
        planned_minutes: The todo's estimate when it carries one, else ``None``
            (the module's soft-block default then governs the gentle check).
        source: ``"todo"`` when taken from an open loop, ``"default"`` otherwise.
    """

    intended_task: str
    planned_minutes: float | None
    source: str
    todo_id: int | None = None


def infer_focus_start(candidates: list[dict[str, Any]]) -> InferredFocus:
    """Infer a focus session's task + duration for a no-field start.

    Args:
        candidates: Open todo dicts in the order you'd want them picked — ideally
            most-avoided first, then the rest by priority. The first with a
            non-blank title wins.

    Returns:
        An :class:`InferredFocus`. When a todo is chosen its ``id`` rides along as
        ``todo_id`` so the session links back to it (the close episode then
        inherits the todo's energy/category for learning). Always returns
        something (a generic block when ``candidates`` is empty), so the one-tap
        start never dead-ends.
    """
    for todo in candidates:
        title = (todo.get("title") or "").strip()
        if not title:
            continue
        est = todo.get("estimate_minutes")
        planned = (
            float(est)
            if isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0
            else None
        )
        tid = todo.get("id")
        return InferredFocus(title, planned, "todo", tid if isinstance(tid, int) else None)
    return InferredFocus(DEFAULT_FOCUS_TASK, None, "default", None)


# --- Calendar-armed start (zero taps) ----------------------------------------
#
# The most frictionless start is none at all: a "focus" / "deep work" block on
# your calendar *is* the intention — you scheduled it once. So when such an event
# is live and no session is running, arm one for its window automatically, with
# the event's own remaining time as the planned duration.

#: Whole-word cues in a calendar title that mark a self-scheduled focus block.
_FOCUS_INTENT_RE = re.compile(r"\b(?:deep[\s-]*work|focus(?:\s*time)?|heads?[\s-]*down)\b", re.I)
#: The label to strip so "Deep work: the RFC" yields the real task "the RFC".
_FOCUS_LABEL_RE = re.compile(
    r"^\s*(?:deep[\s-]*work|focus(?:\s*time)?|heads?[\s-]*down)\s*[:\-–—]*\s*", re.I
)


def is_focus_intent_title(title: str | None) -> bool:
    """Whether a calendar title reads as a self-scheduled focus/deep-work block."""
    return bool(_FOCUS_INTENT_RE.search(title or ""))


def focus_task_from_title(title: str | None) -> str | None:
    """The task inside a focus event title, or ``None`` when it's just a label.

    "Deep work: the API refactor" → "the API refactor"; a bare "Focus time" →
    ``None`` (so the caller can fall back to your top todo).
    """
    remainder = _FOCUS_LABEL_RE.sub("", (title or "").strip()).strip(" :–—-")
    return remainder or None


def _infer_focus_task(store: MemoryStore, now: datetime) -> InferredFocus:
    """Infer a focus task from open todos (most-avoided first).

    The bare-``Focus time`` fallback: a block with no task in its title protects
    your top open loop instead. Mirrors the focus router's one-tap start ordering
    so the endpoint and CLI arm identically.
    """
    from prefrontal.todos import avoided_todos  # lazy: avoid an import cycle

    open_todos = store.open_todos(exclude_delegated=True)
    avoided = [a["todo"] for a in avoided_todos(open_todos, now)]
    seen = {t["id"] for t in avoided}
    return infer_focus_start(avoided + [t for t in open_todos if t["id"] not in seen])


def arm_focus_session(
    store: MemoryStore, settings: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    """Auto-start a focus session from a *live* calendar focus block (zero taps).

    The shared core behind ``POST /webhooks/focus/arm`` and ``prefrontal focus
    arm`` (so the endpoint and the native launchd tick can't drift): when a
    "focus"/"deep work" event is happening now and no session is already running,
    arm one for the rest of its window — the event's own end is the planned
    duration. A bare "Focus time" (no task in the title) protects your top open
    todo instead. Idempotent: safe to call every tick without stacking sessions.

    Returns the same shape the endpoint returns: ``{"armed": bool, ...}`` — with
    ``session_id``/``intended_task``/``planned_minutes``/``from_commitment`` when
    it armed, or a ``reason`` when it didn't.
    """
    if store.active_focus_sessions():
        return {"armed": False, "reason": "a focus session is already active"}

    if now is None:
        from prefrontal.impact import utcnow  # lazy: keep this module impact-free

        now = utcnow()
    # The candidate focus blocks are today's in the user's *local* day.
    day_start, day_end = local_day_bounds(now, settings.timezone)
    live: dict[str, Any] | None = None
    end_at: datetime | None = None
    for c in store.commitments_between(
        day_start.strftime(TS_FMT), day_end.strftime(TS_FMT)
    ):
        if not is_focus_intent_title(c.get("title")):
            continue
        start = _parse_ts(c.get("start_at"))
        end = _parse_ts(c.get("end_at"))
        if start is None or start > now or end is None or end <= now:
            continue  # not happening right now
        live, end_at = c, end
        break

    if live is None:
        return {"armed": False, "reason": "no live focus block on the calendar"}

    remaining = round((end_at - now).total_seconds() / 60.0, 1)
    task = focus_task_from_title(live.get("title"))
    todo_id = None
    if task is None:  # a bare "Focus time" — protect the top todo instead
        guess = _infer_focus_task(store, now)
        task, todo_id = guess.intended_task, guess.todo_id
    session_id = store.start_focus_session(
        task,
        planned_minutes=remaining if remaining > 0 else None,
        aligned=True,
        todo_id=todo_id,
    )
    return {
        "armed": True,
        "session_id": session_id,
        "intended_task": task,
        "planned_minutes": remaining,
        "from_commitment": live.get("id"),
    }


# --- Proactive "want to focus?" suggestion (opt-in) --------------------------
#
# Even one-tap starting still needs *remembering to tap*. So the coaching tick
# can gently offer to start a block at a ripe moment — but this is a preference,
# not a default assist (an unsolicited "go focus" is easy to resent), so it's
# OFF unless `suggest_focus_start` is `on`. The ripe moment, using only what a
# module may read: it's the focus-friendly part of the day, no session is
# running, and you haven't done a focus block yet today. Debounced to once a day.

#: Default local-hour band in which a start is suggested (morning to early-PM,
#: when deep work tends to land).
DEFAULT_SUGGEST_START_HOUR = 8
DEFAULT_SUGGEST_UNTIL_HOUR = 15
#: Default local-hour band for the end-of-day recap ("log a block I forgot?").
DEFAULT_RECAP_START_HOUR = 17
DEFAULT_RECAP_UNTIL_HOUR = 22


def should_suggest_focus(
    *,
    local_hour: int,
    start_hour: int,
    until_hour: int,
    has_active_session: bool,
    focused_today: bool,
) -> bool:
    """Whether the tick should offer to start a focus block right now (pure).

    ``True`` only in the ``[start_hour, until_hour)`` local band, when nothing is
    already running and no block has happened yet today — so it reads as "want to
    get one in?" rather than nagging someone mid-flow or after a good day's focus.
    """
    if has_active_session or focused_today:
        return False
    return start_hour <= local_hour < until_hour


def build_focus_suggestion(name: str = "") -> str:
    """The gentle offer text; the one-tap start fills in the task when tapped."""
    lead = f"Hey {name}" if name else "Hey"
    return (
        f"{lead} — no focus block yet today. Want to protect one? Tap to start on "
        "whatever you've been putting off, and I'll shield it from the noise."
    )


def should_recap_focus(
    *,
    local_hour: int,
    start_hour: int,
    until_hour: int,
    has_active_session: bool,
    focused_today: bool,
) -> bool:
    """Whether to ask, at day's end, about a focus block you forgot to log (pure).

    ``True`` only in the evening ``[start_hour, until_hour)`` band, when nothing
    is running and nothing was logged today — the case where a heads-down stretch
    likely happened but never got recorded. If a block *was* logged, there's
    nothing to recover, so it stays quiet.
    """
    if has_active_session or focused_today:
        return False
    return start_hour <= local_hour < until_hour


def build_focus_recap(name: str = "") -> str:
    """The end-of-day 'did you focus and forget to log it?' offer text."""
    lead = f"Hey {name}" if name else "Hey"
    return (
        f"{lead} — no focus block logged today. Did you get heads-down on something "
        "I didn't see? Tap to log it after the fact so it still counts."
    )


def _log_switch_episode(store: MemoryStore, closed: dict) -> int | None:
    """Log one ``switch`` episode per closed focus session (Impulsivity, §8).

    Carries the session's switch tally so the ``context_switch`` learning pass can
    derive a baseline: ``predicted_value`` = switch-impulses signalled,
    ``actual_value`` = of those, captured-and-deferred. ``context='focus'`` buckets
    all focus blocks into one pattern; the intention rides in ``notes``. Logged for
    every close (including a clean 0-impulse block, a real data point), so the
    per-session mean isn't biased toward busy sessions.

    Returns the new episode id (episodes never fail silently here).
    """
    impulses = int(closed.get("switch_impulses") or 0)
    deferred = int(closed.get("switches_deferred") or 0)
    intended = closed.get("intended_task")
    return store.log_episode(
        "switch",
        predicted_value=float(impulses),
        actual_value=float(deferred),
        acknowledged=True,
        context="focus",
        notes=f"focus: {intended}" if intended else None,
    )


def record_focus_end(
    store: MemoryStore,
    closed: dict,
    *,
    outcome: str | None = None,
    breadcrumb: str | None = None,
) -> dict:
    """Log a completed focus session as a ``task`` episode for pattern tracking.

    Predicted = the planned duration (may be ``None`` when none was stated),
    actual = minutes actually spent. The episode ``outcome`` comes from the
    one-tap exit rating when given (``worth_it``/``should_have_stopped``/
    ``pulled_off``), else it is inferred from alignment. The breadcrumb and
    rating are preserved verbatim in ``notes`` so re-entry and review can use them.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        closed: A closed session dict (with ``actual_minutes``).
        outcome: Optional one-tap rating.
        breadcrumb: Optional "where I was / next step" note.

    Returns:
        ``{"episode_id": int|None, "outcome": str|None}``.
    """
    actual = closed.get("actual_minutes")
    planned = closed.get("planned_minutes")
    aligned = bool(closed.get("aligned"))
    ep_outcome = FOCUS_OUTCOME.get(outcome) if outcome else None
    if ep_outcome is None:
        ep_outcome = "success" if aligned else "partial"
    notes_parts: list[str] = []
    if outcome:
        notes_parts.append(f"rating: {outcome}")
    if breadcrumb:
        notes_parts.append(f"next: {breadcrumb}")
    energy, category = _todo_tags(store, closed)
    episode_id = store.log_episode(
        "task",
        predicted_value=planned,
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=True,
        context=f"focus: {closed.get('intended_task')}",
        outcome=ep_outcome,
        notes="; ".join(notes_parts) or None,
        energy=energy,
        category=category,
    )
    _log_switch_episode(store, closed)
    return {"episode_id": episode_id, "outcome": ep_outcome}


def record_focus_abandoned(store: MemoryStore, closed: dict) -> dict:
    """Log an abandoned session as a drift ``miss`` (no reliable actual time).

    ``actual_value`` is intentionally ``None`` — a forgotten "surfacing" tap
    means we don't know when focus actually ended — so it contributes to
    ``drift`` without polluting ``time_estimation`` with a fabricated duration.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        closed: The closed (abandoned) session dict.

    Returns:
        ``{"episode_id": int, "outcome": "miss"}``.
    """
    energy, category = _todo_tags(store, closed)
    episode_id = store.log_episode(
        "task",
        predicted_value=closed.get("planned_minutes"),
        actual_value=None,
        acknowledged=False,
        context=f"focus abandoned: {closed.get('intended_task')}",
        outcome="miss",
        energy=energy,
        category=category,
    )
    _log_switch_episode(store, closed)
    return {"episode_id": episode_id, "outcome": "miss"}


@dataclass(frozen=True)
class FocusEval:
    """The interrupt decision for one active focus session.

    The single shared verdict behind both the ``/webhooks/focus/check`` endpoint
    (polled by n8n) and the in-process coaching tick — computed by
    :func:`evaluate_focus_session` (pure) and applied by
    :func:`apply_focus_evaluation` (writes) — so the two never drift.

    Attributes:
        level: The current :func:`focus_level` (``none``/``check``/``break``).
        fire: Whether ``level`` is a *new* rung since the session's ``last_level``
            (so the interrupt fires once per level).
        message: The user-facing interrupt text, built only when ``fire``.
        abandoned: Whether the session has run past the abandon ratio and should
            be auto-closed.
        protect: Whether an aligned, healthy block should currently shield the
            user from other modules' non-critical nudges.
    """

    level: str
    fire: bool
    message: str
    abandoned: bool
    protect: bool


def evaluate_focus_session(
    session: dict,
    *,
    soft_block_minutes: float,
    hard_interrupt_minutes: float,
    abandon_ratio: float,
    protect_enabled: bool,
    name: str = "",
) -> FocusEval:
    """Compute the interrupt decision for one active focus session (pure, no writes).

    An abandoned session short-circuits to ``abandoned=True`` (nothing else is
    meaningful — it's about to be closed). Otherwise the level, the fire-once
    transition, the protect bit, and the message (only when firing) are computed
    from the same pure helpers the endpoint has always used.
    """
    elapsed = session.get("elapsed_minutes") or 0.0
    if is_abandoned(elapsed, hard_interrupt_minutes, abandon_ratio):
        return FocusEval(level="none", fire=False, message="", abandoned=True, protect=False)
    planned = session.get("planned_minutes")
    aligned = bool(session.get("aligned"))
    level = focus_level(
        elapsed,
        planned_minutes=planned,
        soft_block_minutes=soft_block_minutes,
        hard_interrupt_minutes=hard_interrupt_minutes,
    )
    fire = level_rank(level) > level_rank(session.get("last_level") or "none")
    protect = should_protect(level, aligned=aligned, protect_enabled=protect_enabled)
    message = (
        build_focus_message(
            level,
            task=session.get("intended_task") or "",
            elapsed_minutes=elapsed,
            aligned=aligned,
            name=name,
        )
        if fire
        else ""
    )
    return FocusEval(level=level, fire=fire, message=message, abandoned=False, protect=protect)


def apply_focus_evaluation(store: MemoryStore, session: dict, ev: FocusEval) -> dict | None:
    """Apply the writes a :class:`FocusEval` implies; return the abandon record, else ``None``.

    An abandoned session is auto-closed and logged as a drift miss (the record is
    returned so a caller can report its outcome); a firing level advances
    ``last_level`` so the interrupt fires once. Idempotent across callers: whichever
    of the endpoint or the coaching tick runs first advances ``last_level`` (or
    closes the session, dropping it from ``active_focus_sessions``), so the other
    sees no new level and does nothing — no double-fire when both are wired.
    """
    if ev.abandoned:
        closed = store.close_focus_session(session["id"], status="abandoned")
        return record_focus_abandoned(store, closed)
    if ev.fire:
        store.set_focus_session_level(session["id"], ev.level)
    return None


def record_focus_switched(store: MemoryStore, closed: dict) -> dict:
    """Log a focus block the user deliberately switched away from (Impulsivity).

    Distinct from an abandon (a *forgotten* exit): here the user consciously
    honored a switch-impulse, so the block was real but cut short. The outcome is
    ``partial`` — the block happened but didn't run to plan — so it feeds ``drift``.

    ``actual_value`` is intentionally ``None`` (like an abandon), even though the
    end time *is* known here: a block cut short by a deliberate switch stopped
    because the user chose to, not because the estimate was wrong, so its truncated
    duration isn't an estimation-accuracy signal — feeding it would drag
    ``time_estimation`` toward zero. The genuine estimate signal comes from blocks
    that ran to their natural end (:func:`record_focus_end`). The minutes actually
    spent are kept in ``notes`` for provenance.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        closed: The closed (switched) session dict, with ``actual_minutes``.

    Returns:
        ``{"episode_id": int, "outcome": "partial"}``.
    """
    actual = closed.get("actual_minutes")
    energy, category = _todo_tags(store, closed)
    episode_id = store.log_episode(
        "task",
        predicted_value=closed.get("planned_minutes"),
        actual_value=None,
        acknowledged=True,
        context=f"focus switched: {closed.get('intended_task')}",
        outcome="partial",
        notes=f"spent {round(actual, 1)}m before switching" if actual is not None else None,
        energy=energy,
        category=category,
    )
    _log_switch_episode(store, closed)
    return {"episode_id": episode_id, "outcome": "partial"}


def retract_switched_estimates(store: MemoryStore, *, apply: bool) -> dict[str, Any]:
    """Null ``actual_value`` on past deliberately-switched focus blocks (idempotent).

    A one-off backfill matching the fix in :func:`record_focus_switched`: before
    it, a block cut short by a deliberate switch logged its truncated duration as
    ``actual_value``, so it fed ``time_estimation`` and dragged the multiplier
    toward zero. This finds those episodes (a ``task`` whose ``context`` starts with
    ``"focus switched:"`` that still has an ``actual_value``) and clears the
    duration, leaving the ``partial`` outcome intact for ``drift``. ``apply``
    ``False`` is a dry run (counts only). Re-running is a no-op (the cleared rows no
    longer match). Returns ``{scanned, cleared, samples}``.
    """
    scanned = cleared = 0
    samples: list[str] = []
    for ep in store.episodes_by_type("task", limit=1_000_000):
        context = ep.get("context") or ""
        if not context.startswith("focus switched:"):
            continue
        scanned += 1
        if ep.get("actual_value") is None:
            continue
        if apply:
            store.clear_episode_actual_value(ep["id"])
        cleared += 1
        if len(samples) < 5:
            samples.append(context)
    return {"scanned": scanned, "cleared": cleared, "samples": samples}


def is_focus_protected(store: MemoryStore) -> bool:
    """Whether an aligned focus block is currently shielding the user from noise.

    Derived live from the ``focus_sessions`` table and the hyperfocus preferences
    so it needs no separate bookkeeping. Other modules' delivery paths can consult
    this to hold non-critical nudges while productive hyperfocus is in progress
    (a ``break`` or a hard commitment guard should still punch through — those are
    not gated on this).

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.

    Returns:
        ``True`` if any active session is aligned, protection is enabled, and the
        block is below the hard ceiling.
    """
    protect_enabled = store.get_bool("protect_aligned_hyperfocus", True)
    if not protect_enabled:
        return False
    soft = store.get_float("hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
    hard = store.get_float("hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
    for session in store.active_focus_sessions():
        if not session.get("aligned"):
            continue
        level = focus_level(
            session.get("elapsed_minutes") or 0.0,
            planned_minutes=session.get("planned_minutes"),
            soft_block_minutes=soft,
            hard_interrupt_minutes=hard,
        )
        if should_protect(level, aligned=True, protect_enabled=True):
            return True
    return False


# --- Learned personal soft-block length (learning loop) ----------------------
#
# The soft alignment check fires at ``hyperfocus_block_minutes`` (default 90) when
# no per-session duration was stated — a one-size default for everyone. But closed
# focus sessions carry their real duration *and* the one-tap exit rating
# (``worth_it`` / ``should_have_stopped`` / ``pulled_off``), which is exactly the
# signal for where *this* person's productive focus tips into diminishing returns:
# sessions they rate "should have stopped" mark the overrun point, while long
# "worth it" blocks say their good focus simply runs longer than 90. Learn the soft
# point from that — run in the nightly ``learn`` pass, bounded, and never
# overriding a value the user set by hand.

#: A learned soft point never drops below this (a check any earlier is just noise).
MIN_SOFT_BLOCK_MINUTES = 30.0
#: Keep the soft check this far below the hard biological-break ceiling, so the two
#: interrupts never collapse onto the same moment.
SOFT_BLOCK_HARD_MARGIN = 10.0
#: Recent focus ``task`` episodes to consider, the total rated floor before we
#: adapt, and the per-signal floors for the two things we can read.
SOFT_BLOCK_LOOKBACK = 60
MIN_SOFT_BLOCK_SAMPLES = 5
MIN_REGRET_SAMPLES = 2
MIN_WORTH_IT_SAMPLES = 3
#: A move smaller than this reads as noise — hold rather than churn the soft point.
SOFT_BLOCK_DEADBAND = 5.0

#: The one-tap exit rating as recorded verbatim in a focus episode's notes
#: ("rating: should_have_stopped"; see :func:`record_focus_end`).
_RATING_RE = re.compile(r"rating: (\w+)")


def _focus_rating(notes: str | None) -> str | None:
    """The raw one-tap exit rating parsed from a focus episode's notes, or ``None``."""
    m = _RATING_RE.search(notes or "")
    return m.group(1) if m else None


def adapt_soft_block(store: MemoryStore, now: datetime | None = None) -> dict:
    """Learn ``hyperfocus_block_minutes`` from rated focus sessions' durations.

    Reads recent focus ``task`` episodes that carry a real duration *and* a one-tap
    rating (``record_focus_end``'s notes). Sessions rated ``should_have_stopped``
    mark where the user typically overruns — the clearest "diminishing returns"
    signal — so their mean becomes the target when there are enough; otherwise, if
    "worth it" blocks routinely run long, their mean raises the soft point to stop
    interrupting genuinely productive focus at 90. ``pulled_off`` sessions are
    ignored (that's misalignment, not duration). Bounded to
    ``[MIN_SOFT_BLOCK_MINUTES, hard − SOFT_BLOCK_HARD_MARGIN]``, inside a deadband,
    never overriding a hand-set value. Always returns a CLI summary dict —
    ``changed`` is ``False`` (with a ``reason``) when the soft point held.
    """
    default = DEFAULT_SOFT_BLOCK_MINUTES
    current = store.get_float("hyperfocus_block_minutes", default)
    hard = store.get_float("hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
    user_set = (
        (store.all_state().get("hyperfocus_block_minutes", {}) or {}).get("source") == "explicit"
    )

    worth_it: list[float] = []
    regret: list[float] = []
    for ep in store.episodes_by_type("task", limit=SOFT_BLOCK_LOOKBACK):
        context = ep.get("context") or ""
        # Only cleanly-ended blocks carry a real duration; "focus abandoned:" /
        # "focus switched:" episodes null their actual_value by design.
        if not context.startswith("focus:"):
            continue
        actual = ep.get("actual_value")
        if actual is None:
            continue
        rating = _focus_rating(ep.get("notes"))
        if rating == "worth_it":
            worth_it.append(float(actual))
        elif rating == "should_have_stopped":
            regret.append(float(actual))

    rated = len(worth_it) + len(regret)
    if rated < MIN_SOFT_BLOCK_SAMPLES:
        return {
            "soft_block": round(current),
            "changed": False,
            "samples": rated,
            "reason": "not enough rated focus sessions yet",
        }
    if user_set:
        return {
            "soft_block": round(current),
            "changed": False,
            "samples": rated,
            "reason": "held (you set this block length)",
        }

    if len(regret) >= MIN_REGRET_SAMPLES:
        target = sum(regret) / len(regret)
        trend = "you tend to overrun around here"
    elif len(worth_it) >= MIN_WORTH_IT_SAMPLES:
        target = sum(worth_it) / len(worth_it)
        trend = "your productive focus runs longer than the default"
    else:
        return {
            "soft_block": round(current),
            "changed": False,
            "samples": rated,
            "reason": "no clear duration signal yet",
        }

    upper = max(MIN_SOFT_BLOCK_MINUTES, hard - SOFT_BLOCK_HARD_MARGIN)
    suggested = int(round(min(max(target, MIN_SOFT_BLOCK_MINUTES), upper) / 5.0) * 5)
    if abs(suggested - current) <= SOFT_BLOCK_DEADBAND:
        return {
            "soft_block": round(current),
            "changed": False,
            "samples": rated,
            "reason": "soft check lands about right",
        }
    store.set_state("hyperfocus_block_minutes", str(suggested), source="inferred")
    return {"soft_block": suggested, "changed": True, "samples": rated, "reason": trend}


#: Interrupt level → the declared intervention it delivers.
_INTERRUPT_INTERVENTION = {"check": "alignment_check", "break": "biological_break"}
#: Interrupt level → coaching urgency. A soft check is a gentle ``nudge``; a
#: biological ``break`` is ``urgent`` (pushes, may bump to sound if ignored) but
#: not ``critical`` — a break doesn't warrant a phone call. Both pierce protected
#: hyperfocus (they're the sanctioned interrupts) via ``suppressed``'s exception.
_INTERRUPT_URGENCY = {"check": "nudge", "break": "urgent"}


class HyperfocusModule(Module):
    """Protects productive hyperfocus and interrupts misdirected hyperfocus."""

    key = "hyperfocus"
    title = "Hyperfocus"
    challenge = (
        "Sustained intense focus that is a strength when aimed at the right task "
        "but a liability when misdirected or when it overruns basic needs and "
        "commitments."
    )
    # Hyperfocus is the module that *provides* the protection (see
    # :meth:`provides_protection`), so its own alignment-check cue must not be
    # gated by that protection — the soft check is the one sanctioned interrupt
    # allowed during an aligned overrun.
    pierces_protection = True
    default_state = {
        "hyperfocus_block_minutes": str(int(DEFAULT_SOFT_BLOCK_MINUTES)),
        "protect_aligned_hyperfocus": "true",
        "hard_interrupt_minutes": str(int(DEFAULT_HARD_INTERRUPT_MINUTES)),
        # Auto-close a session left open past this multiple of the hard ceiling.
        "focus_abandon_after_ratio": str(DEFAULT_FOCUS_ABANDON_RATIO),
        # Proactive "want to focus?" offer — off by default (a preference, like
        # self-care), with a focus-friendly local-hour band when it is on.
        "suggest_focus_start": "off",
        "focus_suggest_start_hour": str(DEFAULT_SUGGEST_START_HOUR),
        "focus_suggest_until_hour": str(DEFAULT_SUGGEST_UNTIL_HOUR),
        # End-of-day "log a block you forgot?" recap — also opt-in.
        "suggest_focus_recap": "off",
        "focus_recap_start_hour": str(DEFAULT_RECAP_START_HOUR),
        "focus_recap_until_hour": str(DEFAULT_RECAP_UNTIL_HOUR),
    }

    def provides_protection(self, store: MemoryStore) -> bool:
        """Report an aligned deep-work block shielding the user (spec §6).

        The engine OR-s this into ``CoachContext.focus_protected`` so the central
        suppression gate can hold other modules' non-critical cues — the module
        that owns focus is the one that says when it's protected.
        """
        return is_focus_protected(store)

    def channel_targets(self) -> dict[str, str]:
        """Focus-session nudges carry one-tap buttons, keyed by ``session_id``."""
        return {"focus": "session_id"}

    def interventions(self) -> list[Intervention]:
        """Declare the asymmetric protect-vs-interrupt interventions."""
        return [
            Intervention(
                name="protect_window",
                description="Suppress non-critical nudges while aligned hyperfocus is healthy.",
                trigger="an aligned session under the hard-interrupt ceiling",
                status="active",
            ),
            Intervention(
                name="alignment_check",
                description="One gentle check once a block overruns its planned (or default) span.",
                trigger="elapsed time passes the planned duration / soft block",
                status="active",
            ),
            Intervention(
                name="biological_break",
                description="Alignment-independent break for water/food/movement past the ceiling.",
                trigger="elapsed time exceeds the hard-interrupt length",
                status="active",
            ),
            Intervention(
                name="abandoned_auto_close",
                description="Auto-close a session left open far past the hard ceiling.",
                trigger="elapsed time exceeds the abandon ratio with no exit tap",
                status="active",
            ),
            Intervention(
                name="suggest_start",
                description="Gently offer to start a focus block at a ripe moment (opt-in).",
                trigger="no block yet today, nothing running, in the focus-friendly hours",
                status="active",
            ),
            Intervention(
                name="recap_prompt",
                description="At day's end, offer to log a block you forgot to start (opt-in).",
                trigger="evening, nothing running, and no block logged today",
                status="active",
            ),
        ]

    def _session_evals(
        self, store: MemoryStore, ctx: CoachContext
    ) -> list[tuple[dict, FocusEval]]:
        """The shared per-active-session interrupt decision, read from live config.

        The tick-side twin of ``/webhooks/focus/check``'s per-session loop: same
        :func:`evaluate_focus_session`, same preference keys, so the endpoint and
        the coaching tick reach the same verdict. Read-only; :meth:`after_fire`
        applies the writes.
        """
        soft = store.get_float("hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
        hard = store.get_float("hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
        abandon_ratio = store.get_float("focus_abandon_after_ratio", DEFAULT_FOCUS_ABANDON_RATIO)
        protect_enabled = store.get_bool("protect_aligned_hyperfocus", True)
        return [
            (
                session,
                evaluate_focus_session(
                    session,
                    soft_block_minutes=soft,
                    hard_interrupt_minutes=hard,
                    abandon_ratio=abandon_ratio,
                    protect_enabled=protect_enabled,
                    name=ctx.display_name,
                ),
            )
            for session in store.active_focus_sessions()
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Fire focus-session interrupts, or offer a start / recap — all on the tick.

        When a block is running this emits its interrupts — the soft
        ``alignment_check`` once it overruns, the hard ``biological_break`` past the
        ceiling — as **read-only** cues (level advance / auto-close happen in
        :meth:`after_fire`, per the base contract). This is the in-process twin of
        ``/webhooks/focus/check`` (still polled by n8n): the shared decision
        (:func:`evaluate_focus_session`) keeps the two in parity, and the shared
        ``last_level`` guard means whichever runs first wins, so wiring both never
        double-fires — but the tick alone now suffices when n8n isn't running.

        When nothing is running it instead emits the opt-in *start* suggestion
        (``suggest_focus_start``) or end-of-day *recap* (``suggest_focus_recap``);
        those are mutually exclusive with a live block, so the two paths never
        overlap.
        """
        evals = self._session_evals(store, ctx)
        if evals:
            return [
                Cue(
                    module=self.key,
                    intervention=_INTERRUPT_INTERVENTION[ev.level],
                    urgency=_INTERRUPT_URGENCY[ev.level],
                    text=ev.message,
                    context_key="focus",
                    dedup_key=f"focus_interrupt:{session['id']}:{ev.level}",
                    ref={"session_id": session["id"], "level": ev.level},
                )
                for session, ev in evals
                if ev.fire and not ev.abandoned
            ]
        local = local_datetime(ctx.now, ctx.timezone)
        focused_today = self._focused_today(store, local.date(), ctx.timezone)
        day = local.date().isoformat()

        def _on(key: str) -> bool:
            return (store.get_state(key, "off") or "off") == "on"

        if _on("suggest_focus_start") and should_suggest_focus(
            local_hour=local.hour,
            start_hour=int(store.get_float("focus_suggest_start_hour", DEFAULT_SUGGEST_START_HOUR)),
            until_hour=int(store.get_float("focus_suggest_until_hour", DEFAULT_SUGGEST_UNTIL_HOUR)),
            has_active_session=False,
            focused_today=focused_today,
        ):
            return [
                Cue(
                    module=self.key,
                    intervention="suggest_start",
                    urgency="nudge",
                    text=build_focus_suggestion(ctx.display_name),
                    context_key="focus",
                    dedup_key=f"focus_suggest:{day}",
                    ref={"suggest": "focus_start"},
                )
            ]

        if _on("suggest_focus_recap") and should_recap_focus(
            local_hour=local.hour,
            start_hour=int(store.get_float("focus_recap_start_hour", DEFAULT_RECAP_START_HOUR)),
            until_hour=int(store.get_float("focus_recap_until_hour", DEFAULT_RECAP_UNTIL_HOUR)),
            has_active_session=False,
            focused_today=focused_today,
        ):
            return [
                Cue(
                    module=self.key,
                    intervention="recap_prompt",
                    urgency="nudge",
                    text=build_focus_recap(ctx.display_name),
                    context_key="focus",
                    dedup_key=f"focus_recap:{day}",
                    ref={"suggest": "focus_log"},
                )
            ]
        return []

    def after_fire(
        self, store: MemoryStore, decisions: list[Any], ctx: CoachContext
    ) -> None:
        """Apply the writes each active session's interrupt decision implies.

        Held back from :meth:`evaluate` so that stays a pure read (base contract).
        Re-runs the identical per-session decision and performs the writes via
        :func:`apply_focus_evaluation`: the one-fire ``last_level`` advance for a
        firing session, and the cue-less auto-close of an abandoned one (which must
        run whether or not a nudge fired — the backstop that stops a forgotten
        session lingering active when n8n isn't polling ``/webhooks/focus/check``).
        Same evaluate-then-apply the endpoint runs inline; the shared functions keep
        them in parity.
        """
        for session, ev in self._session_evals(store, ctx):
            apply_focus_evaluation(store, session, ev)

    @staticmethod
    def _focused_today(store: MemoryStore, today, tz: str) -> bool:
        """Whether any focus session already started on ``today`` (local date)."""
        for session in store.recent_focus_sessions(limit=20):
            started = session.get("started_at")
            if not started:
                continue
            try:
                dt = datetime.strptime(str(started)[:19], TS_FMT)
            except ValueError:
                continue
            if local_datetime(dt, tz).date() == today:
                return True
        return False

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize how hyperfocus is being handled and recent focus blocks."""
        lines: list[str] = []
        soft = store.get_state("hyperfocus_block_minutes")
        hard = store.get_state("hard_interrupt_minutes")
        protect = store.get_bool("protect_aligned_hyperfocus", True)
        if soft and hard:
            stance = (
                "Protect aligned hyperfocus" if protect else "Interrupt on the soft cadence"
            )
            lines.append(
                f"{stance}: soft check at **{soft} min**, hard break at **{hard} min**."
            )
        lines.append(
            "Distinguish good vs bad hyperfocus before interrupting — only redirect "
            "off-task or need-overrunning blocks."
        )

        sessions = store.recent_focus_sessions(limit=100)
        ended = [s for s in sessions if s.get("ended_at") and s.get("status") == "ended"]
        if ended:
            overran = sum(
                1
                for s in ended
                if (s.get("planned_minutes") or 0)
                and minutes_between(s.get("started_at"), s.get("ended_at"), default=0.0)
                > s["planned_minutes"]
            )
            lines.append(
                f"{len(ended)} recent focus session(s) logged; {overran} ran past "
                "their stated plan."
            )

        long_blocks = [
            t
            for t in store.episodes_by_type("task", limit=100)
            if (t.get("actual_value") or 0) and (t.get("predicted_value") or 0)
            and t["actual_value"] >= 2 * t["predicted_value"]
        ]
        if long_blocks:
            lines.append(
                f"{len(long_blocks)} recent block(s) ran 2x+ their estimate — "
                "candidate hyperfocus episodes to review."
            )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(HyperfocusModule())
