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
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from prefrontal.coaching import CoachContext, Cue
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module, ModuleStore
from prefrontal.modules.registry import register

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


def _local(now: datetime, tz: str) -> datetime:
    """A naive-UTC instant as tz-aware local (falls back to UTC)."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return now.replace(tzinfo=timezone.utc).astimezone(zone)


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
    return {"episode_id": episode_id, "outcome": "miss"}


def record_focus_switched(store: MemoryStore, closed: dict) -> dict:
    """Log a focus block the user deliberately switched away from (Impulsivity).

    Distinct from an abandon (a *forgotten* exit): here the user consciously
    honored a switch-impulse, so the block was real but cut short. ``actual_value``
    is the time genuinely spent (known, unlike an abandon), and the outcome is
    ``partial`` — the block happened but didn't run to plan. Feeds
    ``time_estimation`` like any other focus close.

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
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=True,
        context=f"focus switched: {closed.get('intended_task')}",
        outcome="partial",
        energy=energy,
        category=category,
    )
    return {"episode_id": episode_id, "outcome": "partial"}


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


def _minutes_between(start: str | None, end: str | None) -> float:
    """Minutes between two ``YYYY-MM-DD HH:MM:SS`` UTC timestamps (0 if unknown)."""
    if not start or not end:
        return 0.0
    from datetime import datetime

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
    except ValueError:
        return 0.0
    return delta.total_seconds() / 60.0


class HyperfocusModule(Module):
    """Protects productive hyperfocus and interrupts misdirected hyperfocus."""

    key = "hyperfocus"
    title = "Hyperfocus"
    challenge = (
        "Sustained intense focus that is a strength when aimed at the right task "
        "but a liability when misdirected or when it overruns basic needs and "
        "commitments."
    )
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
    }

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
        ]

    def evaluate(self, store: ModuleStore, ctx: CoachContext) -> list[Cue]:
        """Offer a one-tap focus start at a ripe moment (opt-in; see should_suggest_focus).

        Interrupts stay on ``/webhooks/focus/check``; this only emits the gentle
        *start* suggestion, and only when ``suggest_focus_start`` is ``on``.
        """
        if (store.get_state("suggest_focus_start", "off") or "off") != "on":
            return []
        if store.active_focus_sessions():
            return []
        local = _local(ctx.now, ctx.timezone)
        if not should_suggest_focus(
            local_hour=local.hour,
            start_hour=int(store.get_float("focus_suggest_start_hour", DEFAULT_SUGGEST_START_HOUR)),
            until_hour=int(store.get_float("focus_suggest_until_hour", DEFAULT_SUGGEST_UNTIL_HOUR)),
            has_active_session=False,
            focused_today=self._focused_today(store, local.date(), ctx.timezone),
        ):
            return []
        return [
            Cue(
                module=self.key,
                intervention="suggest_start",
                urgency="nudge",
                text=build_focus_suggestion(ctx.display_name),
                context_key="focus",
                dedup_key=f"focus_suggest:{local.date().isoformat()}",
                ref={"suggest": "focus_start"},
            )
        ]

    @staticmethod
    def _focused_today(store: ModuleStore, today, tz: str) -> bool:
        """Whether any focus session already started on ``today`` (local date)."""
        for session in store.recent_focus_sessions(limit=20):
            started = session.get("started_at")
            if not started:
                continue
            try:
                dt = datetime.strptime(str(started)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if _local(dt, tz).date() == today:
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
                and _minutes_between(s.get("started_at"), s.get("ended_at"))
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
