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

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from prefrontal.packs.base import Pack, SituationTool
from prefrontal.packs.registry import register

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore


def _school_run_situation(store: MemoryStore) -> dict[str, Any]:
    """When to leave for the kids' upcoming things — the school-run leave-by.

    Composes the departure primitive (:func:`plan_upcoming_departures`), the same
    engine behind the standing departure nudge, and narrows it to the parent's
    ``child`` commitments — a kid's school event, lesson, or appointment. Read-only:
    it plans from the store's last *fresh* fix (falling back to each commitment's
    static lead) and never fires or records a nudge, so a parent can ask "when do I
    leave?" on demand without arming the reminder.

    Returns a ``headline`` (the most urgent leave-by phrased for a push, empty when
    nothing is pressing) plus every upcoming child departure with its leave-by.
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


#: The Parent pack's one situation tool: the school-run leave-by (see the handler).
SCHOOL_RUN_TOOL = SituationTool(
    key="school_run",
    title="School run",
    description="When to leave for the kids' upcoming school events and appointments.",
    handler=_school_run_situation,
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
    # The on-demand situations this pack answers from live data (read-only).
    situations=(SCHOOL_RUN_TOOL,),
)

register(PARENT_PACK)
