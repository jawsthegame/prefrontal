"""The queryable behavioral model — entity-scoped continuity, retrieved on demand.

The profile summarizer (:mod:`prefrontal.memory.summarizer`) renders one
*population-level* snapshot: the patterns and preferences learned across all of a
user's episodes, prose-summarized and prepended to every agent prompt. That's the
right shape for "who is this person, in general", but it can't answer a question
scoped to *one thing* — "how have they treated *this* todo?".

This module is the other half: a **queryable** behavioral model. Given a single
entity (today: a todo), it reads that entity's longitudinal history — the
``todo_events`` change log — and returns structured facts plus ready-to-inject,
second-person context lines an agent can retrieve when it's about to act on that
entity:

    "You've rescheduled this four times (most recently 3 days ago), and snoozed it
    twice. It's been open 26 days."

That continuity — the system *remembering how you've actually handled a specific
open loop* — is the external-brain payoff a generic reminder app can't match: it
overwrites the deadline in place and forgets you ever moved it. Here the memory is
kept (see the ``todo_events`` table) and made retrievable.

The output is deterministic and grounded in real counts — no model call — so it's
cheap enough to fetch per reminder and stable enough to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from prefrontal.clock import parse_ts, utcnow

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore


def _count_phrase(n: int) -> str:
    """Render a repetition count in natural prose: ``once`` / ``twice`` / ``N times``."""
    if n == 1:
        return "once"
    if n == 2:
        return "twice"
    return f"{n} times"


def _ago_phrase(then: datetime, now: datetime) -> str:
    """Compact "how long ago" phrasing for a past instant (``today`` … ``N weeks ago``)."""
    days = (now - then).total_seconds() / 86400.0
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        # Truncate rather than round: rounding 13.6 up to 14 would print "14 days
        # ago" from inside the < 14 band, crossing into what the weeks band renders.
        return f"{int(days)} days ago"
    return f"{round(days / 7.0)} weeks ago"


@dataclass(frozen=True)
class TodoBehavior:
    """The behavioral model for one todo: its longitudinal facts + context lines.

    Attributes:
        todo_id: The todo this describes.
        title: The todo's current title (so an agent can name the item).
        status: ``open`` / ``done`` / ``dropped``.
        reschedule_count: How many times the deadline has been moved.
        defer_count: How many times the todo has been consciously parked (snoozed).
        days_open: Days since the todo was created (``None`` if unknown).
        last_rescheduled_ago: Human "how long ago" for the most recent reschedule,
            or ``None`` if never rescheduled.
        last_deferred_ago: Human "how long ago" for the most recent defer, or
            ``None`` if never deferred.
        currently_snoozed: Whether the todo is parked to a still-future instant.
        estimate_bias: The learned time-estimation multiplier that applies to a
            task like this (``1.0`` = calibrated), or ``None`` if not enough signal.
        context_lines: Ready-to-inject, second-person lines summarizing the above,
            ordered most-actionable first. Empty when there's nothing worth saying.
    """

    todo_id: int
    title: str
    status: str
    reschedule_count: int
    defer_count: int
    days_open: float | None
    last_rescheduled_ago: str | None
    last_deferred_ago: str | None
    currently_snoozed: bool
    estimate_bias: float | None
    context_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-friendly dict of the facts, for the HTTP query surface."""
        return {
            "todo_id": self.todo_id,
            "title": self.title,
            "status": self.status,
            "reschedule_count": self.reschedule_count,
            "defer_count": self.defer_count,
            "days_open": (
                round(self.days_open, 1) if self.days_open is not None else None
            ),
            "last_rescheduled_ago": self.last_rescheduled_ago,
            "last_deferred_ago": self.last_deferred_ago,
            "currently_snoozed": self.currently_snoozed,
            "estimate_bias": self.estimate_bias,
            "context_lines": list(self.context_lines),
        }

    @property
    def context(self) -> str:
        """The context lines joined into a single paragraph (``""`` when empty)."""
        return " ".join(self.context_lines)


def _resolve_estimate_bias(store: MemoryStore, todo: dict[str, Any]) -> float | None:
    """The *task-specific* time-estimation multiplier for this todo, or ``None``.

    Deliberately narrower than :func:`prefrontal.memory.patterns.resolve_bias`: it
    consults only the energy- and category-specific learned keys, **not** the
    global ``time_estimation_bias`` fallback. The global multiplier is
    population-level — it belongs in the ``/profile`` snapshot, not on every single
    todo — so surfacing it here would attach the same generic line to items with no
    entity-specific signal. Precedence matches ``resolve_bias`` (energy, then
    category); a value within 0.1 of ``1.0`` is no deviation worth naming, so it
    maps to ``None``.
    """
    from prefrontal.memory.patterns import (
        CATEGORY_BIAS_PREFIX,
        ENERGY_BIAS_PREFIX,
    )

    def _normalized(value: Any) -> str | None:
        return (str(value).strip().lower() or None) if value is not None else None

    for prefix, raw in (
        (ENERGY_BIAS_PREFIX, todo.get("energy")),
        (CATEGORY_BIAS_PREFIX, todo.get("category")),
    ):
        tag = _normalized(raw)
        if tag is None:
            continue
        stored = store.get_state(f"{prefix}{tag}")
        if not stored:
            continue
        try:
            bias = float(stored)
        except ValueError:
            continue
        if abs(bias - 1.0) >= 0.1:
            return bias
    return None


def _bias_line(bias: float, category: str | None) -> str:
    """Render the estimate-bias context line from a resolved multiplier."""
    pct = round(abs(bias - 1.0) * 100)
    scope = f"{category} tasks" if category else "tasks like this"
    if bias > 1.0:
        return (
            f"You tend to underestimate {scope} by ~{pct}% — pad the time and "
            "start earlier than feels necessary."
        )
    return (
        f"You tend to overestimate {scope} by ~{pct}% — it'll likely take less "
        "time than you fear, which can make starting easier."
    )


def todo_behavior(
    store: MemoryStore, todo_id: int, *, now: datetime | None = None
) -> TodoBehavior | None:
    """Assemble the behavioral model for one todo, or ``None`` if it doesn't exist.

    Reads the todo and its ``todo_events`` history and derives the counts, recency,
    and applicable estimate bias, then renders the second-person context lines an
    agent retrieves when acting on the item. Deterministic and model-free.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        todo_id: The todo to describe.
        now: The reference time (defaults to :func:`prefrontal.clock.utcnow`);
            injectable so tests are deterministic.

    Returns:
        A :class:`TodoBehavior`, or ``None`` when there is no such todo for the
        scoped user.
    """
    todo = store.get_todo(todo_id)
    if todo is None:
        return None
    now = now or utcnow()

    reschedules = store.todo_events(todo_id, event_type="rescheduled")
    defers = store.todo_events(todo_id, event_type="deferred")

    created = parse_ts(todo.get("created_at"))
    days_open = (
        max(0.0, (now - created).total_seconds() / 86400.0)
        if created is not None
        else None
    )

    def _last_ago(events: list[dict[str, Any]]) -> str | None:
        if not events:
            return None
        when = parse_ts(events[-1].get("created_at"))
        return _ago_phrase(when, now) if when is not None else None

    # Compute the recency phrases once and reuse them for both the structured
    # fields and the context-line inputs — a single source of truth, so the two
    # can't drift if _last_ago ever changes.
    last_rescheduled_ago = _last_ago(reschedules)
    last_deferred_ago = _last_ago(defers)

    snoozed_until = parse_ts(todo.get("snoozed_until"))
    currently_snoozed = snoozed_until is not None and snoozed_until > now

    estimate_bias = _resolve_estimate_bias(store, todo)

    behavior = TodoBehavior(
        todo_id=todo_id,
        title=todo.get("title") or "",
        status=todo.get("status") or "open",
        reschedule_count=len(reschedules),
        defer_count=len(defers),
        days_open=days_open,
        last_rescheduled_ago=last_rescheduled_ago,
        last_deferred_ago=last_deferred_ago,
        currently_snoozed=currently_snoozed,
        estimate_bias=estimate_bias,
        context_lines=_context_lines(
            reschedule_count=len(reschedules),
            defer_count=len(defers),
            last_rescheduled_ago=last_rescheduled_ago,
            currently_snoozed=currently_snoozed,
            days_open=days_open,
            estimate_bias=estimate_bias,
            category=todo.get("category"),
        ),
    )
    return behavior


def _context_lines(
    *,
    reschedule_count: int,
    defer_count: int,
    last_rescheduled_ago: str | None,
    currently_snoozed: bool,
    days_open: float | None,
    estimate_bias: float | None,
    category: str | None,
) -> list[str]:
    """Render the ordered, second-person context lines from the derived facts.

    Most-actionable first: the repeated-reschedule signal leads (it's the clearest
    "this one is sticky" flag), then the defer count, then age, then any estimate
    bias. A todo with no history and no notable age yields no lines — the caller
    then simply injects nothing, rather than a hollow "no signal" sentence.
    """
    lines: list[str] = []

    if reschedule_count:
        recent = f" (most recently {last_rescheduled_ago})" if last_rescheduled_ago else ""
        lead = f"You've rescheduled this {_count_phrase(reschedule_count)}{recent}"
        # Two-plus reschedules is worth naming as a pattern, gently — the point is
        # to notice avoidance, not to scold.
        if reschedule_count >= 3:
            lines.append(
                f"{lead}. It keeps sliding — worth asking whether the plan, the "
                "size, or the deadline is what's wrong before moving it again."
            )
        else:
            lines.append(f"{lead}.")

    if defer_count:
        parked = " (currently parked)" if currently_snoozed else ""
        lines.append(f"You've snoozed this {_count_phrase(defer_count)}{parked}.")

    # Age is only worth surfacing when the item is genuinely old *and* it has some
    # reschedule/defer history — an old-but-untouched todo isn't the same "you keep
    # circling this" story, and the avoidance surface already covers plain age.
    if days_open is not None and days_open >= 14 and (reschedule_count or defer_count):
        lines.append(f"It's been open {round(days_open)} days.")

    if estimate_bias is not None:
        lines.append(_bias_line(estimate_bias, category))

    return lines


def behavior_nudge_clause(store: MemoryStore, todo_id: int) -> str:
    """A compact reschedule/snooze continuity clause to fold into a nudge, or ``""``.

    The nudge-sized slice of the behavioral model. Where :func:`todo_behavior`
    renders the full retrievable picture (recency, age, estimate bias), a *delivered*
    nudge has room for one short clause — so this names only the reschedule and
    snooze **counts**, the history a plain "12d and counting" age can't convey
    ("You keep putting off X … You've rescheduled it 4 times."). Age and estimate
    bias are omitted on purpose: the nudges that use this already state the age, and
    a multi-clause line would bury the concrete ask.

    Returned as a leading-space-prefixed sentence the caller appends
    unconditionally — the same contract as :func:`prefrontal.coaching.note_hint` —
    so an empty history is simply ``""``. Reads counts directly (no bias/age work),
    keeping it cheap enough for the per-tick coaching path.
    """
    reschedules = store.count_todo_events(todo_id, "rescheduled")
    defers = store.count_todo_events(todo_id, "deferred")
    parts: list[str] = []
    if reschedules:
        parts.append(f"rescheduled it {_count_phrase(reschedules)}")
    if defers:
        parts.append(f"snoozed it {_count_phrase(defers)}")
    if not parts:
        return ""
    return f" You've {' and '.join(parts)}."


def behavior_digest_suffix(store: MemoryStore, todo_id: int) -> str:
    """A compact ``· rescheduled N×, snoozed M×`` suffix for a digest line, or ``""``.

    The list-item slice of the behavioral model, for the morning briefing's
    avoidance surfaces. A digest bullet already leads with the item and its age, so
    this stays terse — ``×N`` counts rather than the nudge's full sentence — and
    reinforces *why* something on the "keeps sliding" / "time to decide" lists is
    worth a decision: it's not just old, you've actively moved it.

    Returned with a leading ``" · "`` separator so a caller appends it
    unconditionally (empty history → ``""``), the same append-anywhere contract as
    :func:`behavior_nudge_clause`. Counts-only, so it's cheap per briefing item.
    """
    reschedules = store.count_todo_events(todo_id, "rescheduled")
    defers = store.count_todo_events(todo_id, "deferred")
    parts: list[str] = []
    if reschedules:
        parts.append(f"rescheduled {reschedules}×")
    if defers:
        parts.append(f"snoozed {defers}×")
    if not parts:
        return ""
    return f" · {', '.join(parts)}"


# -- Commitments -------------------------------------------------------------
# The calendar-side of the behavioral model. A commitment can't be "snoozed" the
# way a todo is, so its only continuity signal is how often the synced event has
# *moved* — the "this appointment keeps shifting" story that a plain calendar
# mirror discards by overwriting start_at in place (see commitment_events).


@dataclass(frozen=True)
class CommitmentBehavior:
    """The behavioral model for one commitment: its reschedule history + lines.

    Attributes:
        commitment_id: The commitment this describes.
        title: The commitment's current title (so an agent can name it).
        reschedule_count: How many times the synced event's start has moved.
        last_rescheduled_ago: Human "how long ago" for the most recent move, or
            ``None`` if never rescheduled.
        context_lines: Ready-to-inject, second-person lines summarizing the above;
            empty when there's nothing worth saying.
    """

    commitment_id: int
    title: str
    reschedule_count: int
    last_rescheduled_ago: str | None
    context_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-friendly dict of the facts, for the HTTP query surface."""
        return {
            "commitment_id": self.commitment_id,
            "title": self.title,
            "reschedule_count": self.reschedule_count,
            "last_rescheduled_ago": self.last_rescheduled_ago,
            "context_lines": list(self.context_lines),
        }

    @property
    def context(self) -> str:
        """The context lines joined into a single paragraph (``""`` when empty)."""
        return " ".join(self.context_lines)


def commitment_behavior(
    store: MemoryStore, commitment_id: int, *, now: datetime | None = None
) -> CommitmentBehavior | None:
    """Assemble the behavioral model for one commitment, or ``None`` if absent.

    Reads the commitment and its ``commitment_events`` history and derives the
    reschedule count, recency, and the second-person context line an agent
    retrieves when acting on the event. Deterministic and model-free.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        commitment_id: The commitment to describe.
        now: Reference time (defaults to :func:`prefrontal.clock.utcnow`).

    Returns:
        A :class:`CommitmentBehavior`, or ``None`` when there is no such
        commitment for the scoped user.
    """
    commitment = store.get_commitment(commitment_id)
    if commitment is None:
        return None
    now = now or utcnow()

    reschedules = store.commitment_events(commitment_id, event_type="rescheduled")
    last_ago: str | None = None
    if reschedules:
        when = parse_ts(reschedules[-1].get("created_at"))
        last_ago = _ago_phrase(when, now) if when is not None else None

    lines: list[str] = []
    if reschedules:
        recent = f" (most recently {last_ago})" if last_ago else ""
        lead = (
            f"This has been rescheduled {_count_phrase(len(reschedules))}{recent}"
        )
        if len(reschedules) >= 3:
            lines.append(
                f"{lead}. It keeps moving — worth confirming the time still holds "
                "before you build around it."
            )
        else:
            lines.append(f"{lead}.")

    return CommitmentBehavior(
        commitment_id=commitment_id,
        title=commitment.get("title") or "",
        reschedule_count=len(reschedules),
        last_rescheduled_ago=last_ago,
        context_lines=lines,
    )


def commitment_nudge_clause(store: MemoryStore, commitment_id: int) -> str:
    """A compact reschedule continuity clause to fold into a commitment nudge, or ``""``.

    The commitment analogue of :func:`behavior_nudge_clause` — leading-space-prefixed
    so a departure/prep reminder can append it unconditionally ("… — heads up, this
    has moved 3× on the calendar."). Counts-only and cheap; empty history → ``""``.
    """
    reschedules = store.count_commitment_events(commitment_id, "rescheduled")
    if not reschedules:
        return ""
    return f" Heads up — this has moved {reschedules}× on the calendar."


def commitment_digest_suffix(store: MemoryStore, commitment_id: int) -> str:
    """A compact ``· moved N×`` suffix for a commitment digest line, or ``""``.

    The commitment analogue of :func:`behavior_digest_suffix`, for the morning
    briefing's schedule ("📅 Today") list: a schedule row already leads with the
    time and title, so this stays terse — just the move count, the "this one has
    been shifting" signal a static calendar line can't show. Leading ``" · "``
    separator so a caller appends it unconditionally (empty history → ``""``).
    """
    reschedules = store.count_commitment_events(commitment_id, "rescheduled")
    return f" · moved {reschedules}×" if reschedules else ""
