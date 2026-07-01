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

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
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
                "be doing? Keep going, or open End focus to wrap up."
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
    episode_id = store.log_episode(
        "task",
        predicted_value=planned,
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=True,
        context=f"focus: {closed.get('intended_task')}",
        outcome=ep_outcome,
        notes="; ".join(notes_parts) or None,
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
    episode_id = store.log_episode(
        "task",
        predicted_value=closed.get("planned_minutes"),
        actual_value=None,
        acknowledged=False,
        context=f"focus abandoned: {closed.get('intended_task')}",
        outcome="miss",
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
    episode_id = store.log_episode(
        "task",
        predicted_value=closed.get("planned_minutes"),
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=True,
        context=f"focus switched: {closed.get('intended_task')}",
        outcome="partial",
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
        ]

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
