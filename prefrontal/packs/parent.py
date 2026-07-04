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

from prefrontal.packs.base import Pack
from prefrontal.packs.registry import register

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
    modules=("time_blindness", "task_paralysis"),
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
)

register(PARENT_PACK)
