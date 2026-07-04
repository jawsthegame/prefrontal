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
    # scheduler reads these `todo_window:<category>` coaching keys). Absent-only,
    # so a user's own window override always wins.
    coaching_defaults=MappingProxyType(
        {
            "todo_window:school": "08:00-15:00",
            "todo_window:childcare": "06:00-20:00",
        }
    ),
)

register(PARENT_PACK)
