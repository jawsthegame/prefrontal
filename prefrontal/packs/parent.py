"""The Parent Context Pack.

Managing kids alongside your own executive-function challenges: school runs,
pack-the-bag, a shared family calendar, replanning around a sick day. The
household backbone this composes over — the shared co-parent sheet, star charts,
shopping list, delta digest, load-balance view, and the ``/kids`` surface — has
already shipped; this pack is the declarative layer that turns those into an
installable life-context: it lights up the challenge modules a parent leans on,
seeds parent vocabulary, and sets sensible time windows for parent categories.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from prefrontal.packs.base import Pack, SituationTool
from prefrontal.packs.registry import register

if TYPE_CHECKING:
    from prefrontal.integrations import Generator
    from prefrontal.memory.store import MemoryStore

#: How far ahead the pack-the-bag checklist looks for the kids' events — far
#: enough to prep the night before an early start, short enough that the list
#: stays "what's next" rather than the whole week.
PACK_THE_BAG_HORIZON_HOURS = 36.0

#: Cap on events in one pack-the-bag checklist. A get-ready list longer than a
#: handful is the overwhelm the tool is meant to defuse, not a help.
MAX_PACK_THE_BAG_EVENTS = 3


def _school_run_situation(store: MemoryStore, *, client: Generator | None = None) -> dict[str, Any]:
    """When to leave for the kids' upcoming things — the school-run leave-by.

    Composes the departure primitive (:func:`plan_upcoming_departures`), the same
    engine behind the standing departure nudge, and narrows it to the parent's
    ``child`` commitments — a kid's school event, lesson, or appointment. Read-only:
    it plans from the store's last *fresh* fix (falling back to each commitment's
    static lead) and never fires or records a nudge, so a parent can ask "when do I
    leave?" on demand without arming the reminder.

    Returns a ``headline`` (the most urgent leave-by phrased for a push, empty when
    nothing is pressing) plus every upcoming child departure with its leave-by.
    Fully deterministic — the ``client`` is accepted for the uniform tool contract
    and unused here.
    """
    from prefrontal.commitments import KIND_CHILD
    from prefrontal.departure import (
        build_departure_message,
        next_departure,
        plan_upcoming_departures,
    )

    runs = [
        plan
        for plan in plan_upcoming_departures(store)
        if plan.commitment.get("kind") == KIND_CHILD
    ]
    top = next_departure(runs)
    return {
        "tool": "school_run",
        "title": "School run",
        "headline": build_departure_message(top) if top is not None else "",
        "departures": [
            {
                "commitment_id": plan.commitment["id"],
                "title": plan.commitment.get("title"),
                "location": plan.commitment.get("location"),
                "start_at": plan.commitment.get("start_at"),
                "leave_by": plan.leave_by,
                "minutes_until_leave": plan.minutes_until_leave,
                "travel_minutes": plan.travel_minutes,
                "basis": plan.basis,
                "level": plan.level,
                "message": build_departure_message(plan),
            }
            for plan in runs
        ],
    }


#: The Parent pack's school-run situation tool: the leave-by (see the handler).
SCHOOL_RUN_TOOL = SituationTool(
    key="school_run",
    title="School run",
    description="When to leave for the kids' upcoming school events and appointments.",
    handler=_school_run_situation,
)


def _pack_the_bag_situation(
    store: MemoryStore, *, client: Generator | None = None
) -> dict[str, Any]:
    """A get-ready checklist for the kids' next events — pack-the-bag, decomposed.

    The morning scramble stalls on *starting* ("get them ready" is a wall, not an
    action), so this composes the task-paralysis initiation lever
    (:func:`prefrontal.todos.decompose_task`) over the parent's imminent ``child``
    commitments: for each of the next :data:`MAX_PACK_THE_BAG_EVENTS` kid events
    within :data:`PACK_THE_BAG_HORIZON_HOURS` (far enough ahead to pack the night
    before), it breaks "pack the bag and get ready for <event>" into one tiny first
    step plus the remaining prep steps. So the whole get-out-the-door is an ordered
    list you can start on, one move at a time.

    Read-only: it only *plans* the checklist and never writes. With a model
    ``client`` the steps are event-specific (swim kit, permission slip); with none
    they fall back to the deterministic first-step heuristic, exactly like every
    other decomposition surface. ``fyi`` and placeholder events are excluded (see
    :func:`prefrontal.commitments.is_attendable`) — you don't pack a bag for
    something you're not taking a kid to.

    Returns a ``headline`` (the nearest event's first step, phrased for a push,
    empty when no kid event is coming up) plus a ``checklists`` entry per event.
    """
    from prefrontal.clock import parse_ts
    from prefrontal.commitments import KIND_CHILD, is_attendable
    from prefrontal.impact import utcnow
    from prefrontal.todos import decompose_task

    now = utcnow()
    horizon = now + timedelta(hours=PACK_THE_BAG_HORIZON_HOURS)

    events: list[dict[str, Any]] = []
    for c in store.upcoming_commitments(now=now):
        if c.get("kind") != KIND_CHILD or not is_attendable(c):
            continue
        start = parse_ts(c.get("start_at"))
        if start is None or start > horizon:
            continue
        events.append(c)
        if len(events) >= MAX_PACK_THE_BAG_EVENTS:
            break

    checklists = []
    for c in events:
        title = c.get("title") or "the kids' event"
        plan = decompose_task(f"Pack the bag and get ready for {title}", client=client)
        checklists.append(
            {
                "commitment_id": c.get("id"),
                "title": title,
                "start_at": c.get("start_at"),
                "first_step": plan.first_step,
                "steps": plan.steps,
                "source": plan.source,
            }
        )

    headline = ""
    if checklists:
        top = checklists[0]
        headline = f"Getting {top['title']} ready — start here: {top['first_step']}"
    return {
        "tool": "pack_the_bag",
        "title": "Pack the bag",
        "headline": headline,
        "checklists": checklists,
    }


#: The Parent pack's pack-the-bag situation tool (see the handler).
PACK_THE_BAG_TOOL = SituationTool(
    key="pack_the_bag",
    title="Pack the bag",
    description="A one-step-at-a-time get-ready checklist for the kids' next events.",
    handler=_pack_the_bag_situation,
)


def _sick_day_situation(
    store: MemoryStore, *, client: Generator | None = None
) -> dict[str, Any]:
    """Replan a day upended by a kid home sick — what's immovable, what can slide.

    A sick day collapses your normal windows, and the freeze is not knowing what
    still *has* to happen versus what you can let go. Composes two read-only
    primitives:

    * :func:`prefrontal.panic.build_panic` for the ruthless triage — the single
      calm first step and the count of what is *genuinely* pressing right now; and
    * today's own attendable commitments split by hardness into **must_cover** (a
      firm ``hard`` obligation you still have to attend or line up cover for) and
      **can_reschedule** (soft blocks you can drop to be home with the kid).

    Returns that split plus the panic first step and a one-line ``headline``, so a
    parent sees the reshaped day at a glance from the notification without opening
    the dashboard. Read-only, and fully deterministic — the ``client`` is accepted
    for the uniform tool contract and unused (the panic triage is model-free).
    """
    from prefrontal.clock import TS_FMT, local_day_bounds, parse_ts
    from prefrontal.commitments import HARDNESS_HARD, is_attendable
    from prefrontal.config import get_settings
    from prefrontal.impact import utcnow
    from prefrontal.panic import build_panic

    now = utcnow()
    plan = build_panic(store, now=now)

    day_start, day_end = local_day_bounds(now, get_settings().timezone)
    must_cover: list[dict[str, Any]] = []
    can_reschedule: list[dict[str, Any]] = []
    for c in store.commitments_between(day_start.strftime(TS_FMT), day_end.strftime(TS_FMT)):
        if not is_attendable(c):
            continue
        end = parse_ts(c.get("end_at"))
        if end is not None and end < now:
            continue  # already over — nothing left to replan around
        row = {
            "commitment_id": c.get("id"),
            "title": c.get("title"),
            "start_at": c.get("start_at"),
            "hardness": c.get("hardness"),
        }
        (must_cover if c.get("hardness") == HARDNESS_HARD else can_reschedule).append(row)

    pressing = plan.counts.get("pressing", 0)
    if must_cover:
        n = len(must_cover)
        lead = f"{n} thing{'' if n == 1 else 's'} still needs covering today"
    elif can_reschedule:
        lead = "nothing's locked in — you can clear the day to be home"
    else:
        lead = "your day's already clear"
    tail = f" Start here: {plan.first_step}" if plan.first_step else ""
    headline = f"Kid's home sick — {lead}.{tail}"

    return {
        "tool": "sick_day",
        "title": "Sick-day replan",
        "headline": headline,
        "first_step": plan.first_step,
        "first_step_for": plan.first_step_for,
        "pressing": pressing,
        "must_cover": must_cover,
        "can_reschedule": can_reschedule,
    }


#: The Parent pack's sick-day-replan situation tool (see the handler).
SICK_DAY_TOOL = SituationTool(
    key="sick_day",
    title="Sick-day replan",
    description="Kid home sick: what still has to happen today, what can slide, one next step.",
    handler=_sick_day_situation,
)

#: The Parent pack. Enable with ``PREFRONTAL_PACKS=parent``.
PARENT_PACK = Pack(
    key="parent",
    title="Parent",
    description=(
        "Managing kids alongside your own executive-function challenges — school "
        "runs, pack-the-bag, a shared calendar, and replanning around a sick day."
    ),
    # A parent leans hardest on getting-out-the-door timing and on starting
    # dreaded logistics, so the pack turns these on (atop PREFRONTAL_MODULES).
    # ``trip_tracking`` is switched on for the same reason the focus-balance
    # defaults below are seeded: the guardrail lives *entirely* in that module —
    # it both detects the passive round trips the rollup measures and fires the
    # weekly "light on kids/personal" nudge the ``focus_balance_nudge`` flag arms.
    # Without it, a parent who sets an explicit PREFRONTAL_MODULES list would get
    # the targets and flag seeded but no trips ever tracked and no nudge — the
    # config would be inert (see focus_balance defaults in coaching_defaults).
    modules=("time_blindness", "task_paralysis", "trip_tracking"),
    # Parent-life todo buckets (also the keys the windows below shape).
    categories=("school", "childcare", "household"),
    # ``child`` is already a valid commitment kind; declared here as pack vocab.
    commitment_kinds=("child",),
    # Keep parent-category work inside daytime family windows by default (the
    # scheduler reads these `todo_window:<category>` coaching keys), and turn on the
    # focus-balance guardrail: closed-loop trips (`prefrontal/focus_balance.py`) roll
    # their out-of-home time up by life-sphere, and these weekly `focus_target:<domain>`
    # aims + the `focus_balance_nudge` flag light the gentle "light on kids/personal
    # this week" heads-up. A parent is exactly who wants to make sure kids-and-family
    # time and personal time keep their share against shop/work — so the pack sets
    # modest weekly aims for kids, home, and personal (minutes of *out-of-home* trips
    # per week, not time at home). Absent-only, so a user's own window/target/flag
    # always wins.
    coaching_defaults=MappingProxyType(
        {
            "todo_window:school": "08:00-15:00",
            "todo_window:childcare": "06:00-20:00",
            "focus_balance_nudge": "1",
            "focus_target:kids": "300",
            "focus_target:home": "120",
            "focus_target:personal": "120",
        }
    ),
    # The on-demand situations this pack answers from live data (read-only), each
    # a thin composition of a shipped primitive: the school-run leave-by (departure
    # engine), the pack-the-bag checklist (task decomposition), and the sick-day
    # replan (panic triage + a hard/soft split of today).
    situations=(SCHOOL_RUN_TOOL, PACK_THE_BAG_TOOL, SICK_DAY_TOOL),
)

register(PARENT_PACK)
