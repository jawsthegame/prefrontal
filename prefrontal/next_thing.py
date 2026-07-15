"""One next thing — the single honest next action, never the whole mountain.

Panic mode (:mod:`prefrontal.panic`) exists for the moment you're already frozen:
it names every fire, then hands back one first step. This is its quieter,
always-on sibling — the thing a home- or lock-screen glance shows *before* the
freeze. It answers exactly one question, and only one:

    **What is the single next thing to do right now?**

Not a list. Not a count. One action, with an honest reason attached — because a
flat list of everything you owe is an indictment ("look how far behind you are"),
and one thing is an invitation ("start here"). The whole design constraint is
*subtraction*: whatever else is on the plate is deliberately withheld, reduced to
a single reassuring "and N more can wait".

It reuses Prefrontal's existing honest prioritization rather than inventing a new
one — the same levers the app already trusts:

* the **mid-flight task pinned** — if you're in a focus session or out on an
  errand, that *is* your next thing; the glance reflects it back instead of
  yanking you toward something new (a switch you didn't ask for is the opposite
  of help). The one exception is a commitment you must physically leave for
  *now*, which overrides even flow.
* the **avoided-but-important todo** — when nothing has a hard clock, it surfaces
  the thing you keep skipping (:func:`prefrontal.todos.avoided_todos` via
  :func:`prefrontal.scheduling.suggest_now`), not the shiny easy one.
* the honest **panic prioritization** (:func:`prefrontal.panic.build_panic`) for
  everything with a clock — an overdue todo, a person past-due waiting on you,
  urgent mail — scored exactly as panic scores them, so the glance and the panic
  triage never disagree about what's worst.

The resolution ladder, highest first:

1. **Leave now** — a commitment whose travel-aware departure says *go*. Physical
   and time-critical; overrides even a mid-flight task.
2. **Mid-flight focus** — you're in a focus block. Finish it.
3. **Mid-flight outing** — you're out. The next move is that trip.
4. **A fire you're already behind on** — the worst overdue todo / past-due
   blocker (panic's ``late`` bucket, minus commitments, which tier 1 owns).
5. **Leave soon** — a commitment bearing down within the hour or two.
6. **A fire bearing down** — a due-today todo, urgent mail, an urgent blocker
   (panic's ``soon`` bucket, minus commitments).
7. **The avoided-but-important thing** — panic's ``piling_up``, nothing skipped.
8. **Something that fits right now** — the best todo for your free window.
9. **All clear** — nothing is pressing; that's the message, and it's a good one.

Like panic and the briefing, this is a deterministic, model-free core
(:func:`build_next_thing` / :func:`render_next_thing`), fully testable, with no
LLM in the path — a glance polled every timeline refresh must be fast and never
flake to a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.panic import PanicPlan, Pressure, build_panic
from prefrontal.scheduling import suggest_now


@dataclass(frozen=True)
class NextThing:
    """The single next action to surface, plus the honest reason it was chosen.

    Attributes:
        kind: The source domain — ``focus`` | ``outing`` | ``departure`` |
            ``todo`` | ``blocker`` | ``mail`` | ``clear``.
        reason: *Why this one*, the honesty of the pick — ``leave-now`` |
            ``mid-flight`` | ``overdue`` | ``leave-soon`` | ``due-soon`` |
            ``waiting`` | ``urgent-mail`` | ``avoided`` | ``fits`` | ``clear``.
        title: The one action, phrased to be started (``"Leave for the dentist"``,
            ``"Reply to Sam: budget"``, an avoided todo's own title).
        detail: The honest timing / context subtitle (``"8 min past when you
            should go"``, ``"3d open, kept skipping"``, ``"~15 min · fits now"``).
        source: Where it came from — a calendar or inbox label — so work vs. home
            is visible. ``None`` when unknown.
        action: A hint for the client's one-tap button, mapped to an existing
            App Intent: ``wrap_up`` (focus) | ``im_back`` (outing) | ``leave`` |
            ``start`` (a todo) | ``open`` | ``none``.
        also_count: How many *other* things are on the plate but deliberately not
            shown — the "and N more can wait" reassurance. 0 when this is the
            whole board.
        estimate_minutes: Minutes the thing is expected to take, when known (the
            fits/avoided todo cases).
        free_minutes: Minutes free right now, when the pick is a fits-the-window
            suggestion (so the glance can say "25 min free").
        headline: A single spoken/notification line for a Shortcut or push.
        commitment_id / todo_id / blocker_id / session_id / outing_id: The source
            row id for deep-linking or a one-tap action, when applicable.
    """

    kind: str
    reason: str
    title: str
    detail: str
    action: str = "none"
    source: str | None = None
    also_count: int = 0
    estimate_minutes: int | None = None
    free_minutes: int | None = None
    headline: str = ""
    commitment_id: int | None = None
    todo_id: int | None = None
    blocker_id: int | None = None
    session_id: int | None = None
    outing_id: int | None = None


# --- Helpers -----------------------------------------------------------------

#: Departure levels that mean "leave now" — these override even a mid-flight task.
_GO_LEVELS = frozenset({"go"})
#: Departure levels that mean "leave soon" — bearing down, but flow is still safe.
_SOON_LEVELS = frozenset({"soon", "heads_up"})


def _board_size(plan: PanicPlan) -> int:
    """How many distinct pressing/queued items panic sees (the whole mountain)."""
    return len(plan.late) + len(plan.soon) + len(plan.piling_up)


def _reason_for(p: Pressure) -> str:
    """Map a panic pressure to a next-thing reason string."""
    if p.kind == "blocker":
        return "waiting"
    if p.kind == "mail":
        return "urgent-mail"
    if p.bucket == "late":
        return "overdue"
    if p.bucket == "piling_up":
        return "avoided"
    return "due-soon"


def _from_pressure(p: Pressure, *, also_count: int, headline: str) -> NextThing:
    """Build a :class:`NextThing` from a panic :class:`~prefrontal.panic.Pressure`."""
    return NextThing(
        kind=p.kind,
        reason=_reason_for(p),
        title=p.title,
        detail=p.when,
        action="start" if p.kind == "todo" else "open",
        source=p.source,
        also_count=also_count,
        headline=headline,
        todo_id=p.todo_id,
        blocker_id=p.blocker_id,
        commitment_id=p.commitment_id,
    )


# --- Build -------------------------------------------------------------------


def build_next_thing(
    store: MemoryStore, now: Any | None = None, *, settings: Any | None = None
) -> NextThing:
    """Resolve the single honest next action from memory (deterministic, model-free).

    Walks the ladder in the module docstring and returns the first tier that has
    something. Never returns ``None`` — an empty board is itself an answer
    (``kind="clear"``).

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore` (user-scoped).
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        settings: Optional resolved settings (timezone + enabled modules). Defaults
            to :func:`prefrontal.config.get_settings`, matching ``build_panic``.

    Returns:
        A :class:`NextThing`.
    """
    now = now or utcnow()

    from prefrontal.config import get_settings

    settings = settings or get_settings()

    # Departure is a Time Blindness intervention; when it's off there's no
    # travel-aware leave-by to reason about (matching /departure/next).
    from prefrontal.departure import next_departure, plan_upcoming_departures
    from prefrontal.modules.registry import is_enabled

    top_dep = None
    if is_enabled("time_blindness", settings):
        top_dep = next_departure(plan_upcoming_departures(store))

    # The honest cross-domain prioritizer — the same scoring panic uses. Its
    # commitment pressures are handled by the travel-aware departure engine above
    # (a better leave-by than panic's static lead), so we drop them here.
    plan = build_panic(store, now=now)
    late_fires = [p for p in plan.late if p.kind != "commitment"]
    soon_fires = [p for p in plan.soon if p.kind != "commitment"]

    # The size of the whole board, for the "+N more can wait" reassurance. Panic's
    # buckets already include the upcoming commitment as a "Leave for …" pressure,
    # so the departure isn't added again (that would double-count it).
    board = _board_size(plan)

    # The *most recent* active session/outing — the active-* lists are ordered
    # oldest-first (`ORDER BY id`), so on the (rare) chance more than one is open
    # the latest is the live context; pinning the oldest could surface a stale one.
    focus = store.most_recent_active_focus_session()
    outing = store.most_recent_active_outing()

    # 1. Leave *now* — physical, time-critical, overrides even flow.
    if top_dep is not None and top_dep.level in _GO_LEVELS:
        return _departure_thing(top_dep, reason="leave-now", also_count=max(0, board - 1))

    # 2/3. Mid-flight task pinned — reflect what you're already doing, don't
    # switch you to something new.
    if focus is not None:
        return _focus_thing(focus, also_count=max(0, board))
    if outing is not None:
        return _outing_thing(outing, also_count=max(0, board))

    # 4. A fire you're already behind on (overdue todo / past-due blocker).
    if late_fires:
        top = max(late_fires, key=lambda p: p.score)
        return _from_pressure(
            top, also_count=max(0, board - 1), headline=_headline_for(top)
        )

    # 5. Leave soon — a commitment bearing down (flow still safe, so below fires).
    if top_dep is not None and top_dep.level in _SOON_LEVELS:
        return _departure_thing(top_dep, reason="leave-soon", also_count=max(0, board - 1))

    # 6. A fire bearing down (due-today todo, urgent mail, urgent blocker).
    if soon_fires:
        top = max(soon_fires, key=lambda p: p.score)
        return _from_pressure(
            top, also_count=max(0, board - 1), headline=_headline_for(top)
        )

    # 7. The avoided-but-important thing — nothing has a clock, so name the thing
    # you keep skipping rather than a shiny-but-easy one.
    if plan.piling_up:
        top = plan.piling_up[0]
        return _from_pressure(
            top, also_count=max(0, board - 1), headline=_headline_for(top)
        )

    # 8. Something that fits right now — the best todo for the free window.
    suggestion = suggest_now(store, settings, now)
    s = suggestion.get("suggestion")
    if s is not None:
        free = int(suggestion.get("free_minutes") or 0)
        est = s.get("estimate_minutes")
        est = int(est) if est is not None else None
        parts = []
        if est is not None:
            parts.append(f"~{est} min")
        if free > 0:
            parts.append(f"{free}m free")
        detail = " · ".join(parts) if parts else "you can start now"
        reason = "avoided" if s.get("reason") == "avoided" else "fits"
        title = str(s.get("title") or "")
        return NextThing(
            kind="todo",
            reason=reason,
            title=title,
            detail=detail,
            action="start",
            source=s.get("domain"),
            also_count=max(0, board),
            estimate_minutes=est,
            free_minutes=free or None,
            headline=f"Next: {title} — {detail}.",
            todo_id=s.get("todo_id"),
        )

    # 9. All clear — the board is empty, and that's the message.
    return NextThing(
        kind="clear",
        reason="clear",
        title="Nothing pressing",
        detail="Take a breath, or pick something you want to move.",
        action="none",
        also_count=0,
        headline="Nothing's pressing right now — take a breath.",
    )


def _departure_thing(dep: Any, *, reason: str, also_count: int) -> NextThing:
    """Build a :class:`NextThing` from a :class:`~prefrontal.departure.DeparturePlan`."""
    c = dep.commitment
    title = c.get("title") or "your next commitment"
    location = c.get("location")
    if reason == "leave-now":
        detail = "leave now" + (f" for {location}" if location else "")
        headline = f"Time to go — leave now for {title}."
    else:
        # This branch is the "leave-soon" tier, entered only when the departure
        # level is soon/heads_up — i.e. minutes_until_leave > 0 (go is <= 0). Round
        # *up*, so a fractional 0.4 min doesn't render as "leave in 0 min" (which
        # would read as "leave now" and contradict the soon-not-go semantics).
        mins = max(1, ceil(dep.minutes_until_leave))
        detail = f"leave in {mins} min"
        headline = f"Next: leave for {title} in {mins} min."
    return NextThing(
        kind="departure",
        reason=reason,
        title=f"Leave for {title}",
        detail=detail,
        action="leave",
        source=c.get("calendar"),
        also_count=also_count,
        headline=headline,
        commitment_id=c.get("id"),
    )


def _focus_thing(session: dict[str, Any], *, also_count: int) -> NextThing:
    """Build a mid-flight focus :class:`NextThing` (the session, pinned)."""
    task = (session.get("intended_task") or "").strip() or "your focus session"
    elapsed = session.get("elapsed_minutes")
    detail = f"in progress · {round(elapsed)} min in" if elapsed else "in progress"
    return NextThing(
        kind="focus",
        reason="mid-flight",
        title=task,
        detail=detail,
        action="wrap_up",
        also_count=also_count,
        headline=f"You're mid-flight on {task}. Stay with it.",
        session_id=session.get("id"),
    )


def _outing_thing(outing: dict[str, Any], *, also_count: int) -> NextThing:
    """Build a mid-flight outing :class:`NextThing` (the trip, pinned)."""
    intention = (outing.get("intention") or "").strip() or "your errand"
    return NextThing(
        kind="outing",
        reason="mid-flight",
        title=intention,
        detail="you're out",
        action="im_back",
        also_count=also_count,
        headline=f"You're out: {intention}. Tap when you're back.",
        outing_id=outing.get("id"),
    )


def _headline_for(p: Pressure) -> str:
    """One-line headline for a pressure-sourced next thing."""
    return f"Next: {p.title} — {p.when}."


# --- Render ------------------------------------------------------------------


def render_next_thing(thing: NextThing) -> str:
    """Render a :class:`NextThing` as a short, calm Markdown block (for the CLI).

    Deliberately tiny — one action, its reason, and a single "and N more can wait"
    line. It must never grow into the list it exists to replace.
    """
    reason_label = {
        "leave-now": "Time to go",
        "leave-soon": "Coming up",
        "mid-flight": "You're mid-flight",
        "overdue": "Overdue",
        "due-soon": "Due soon",
        "waiting": "Someone's waiting",
        "urgent-mail": "Urgent mail",
        "avoided": "You keep skipping this",
        "fits": "Fits right now",
        "clear": "All clear",
    }.get(thing.reason, "Next")

    lines = ["# One next thing", ""]
    if thing.kind == "clear":
        lines.append(f"**{thing.title}.** {thing.detail}")
        return "\n".join(lines).rstrip() + "\n"

    lines.append(f"**▶ {thing.title}**")
    src = f" · {thing.source}" if thing.source else ""
    lines.append(f"_{reason_label} — {thing.detail}{src}_")
    if thing.also_count > 0:
        noun = "thing" if thing.also_count == 1 else "things"
        lines.append("")
        lines.append(
            f"_(+{thing.also_count} other {noun} on your plate — they can wait.)_"
        )
    return "\n".join(lines).rstrip() + "\n"
