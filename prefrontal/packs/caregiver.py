"""The Caregiver Context Pack.

Caring for an aging parent, an ill partner, or a disabled family member alongside
your own executive-function challenges: a stream of medical appointments and
medication timing, a mountain of insurance/benefits/legal admin that's easy to
freeze on, and — the risk that defines caregiving — pouring so much into someone
else's needs that your own basic self-care lapses.

Like :mod:`~prefrontal.packs.parent`, this is a declarative composition over
existing primitives, not new machinery: it lights up the challenge modules a
caregiver leans on, seeds caregiver vocabulary, keeps that work inside sensible
daytime windows, and — distinctively — turns the **self-care** checks on, because
the person most likely to skip meals and water is the one doing the caring.

Deliberately declarative and small. The pack-specific *situation tools* and a
caregiver *surface* (the counterparts to the Parent pack's household sheet and
``/kids`` — e.g. a care-recipient facts sheet, a shared-with-siblings care log,
a dedicated ``care`` commitment kind) are the next slice; this establishes the
installable life-context they'll slot into.
"""

from __future__ import annotations

from types import MappingProxyType

from prefrontal.packs.base import Pack
from prefrontal.packs.registry import register

#: The Caregiver pack. Enable with ``PREFRONTAL_PACKS=caregiver`` (or alongside
#: others, e.g. ``PREFRONTAL_PACKS=parent,caregiver`` for the sandwich generation).
CAREGIVER_PACK = Pack(
    key="caregiver",
    title="Caregiver",
    description=(
        "Caring for an aging parent, ill partner, or disabled family member "
        "alongside your own executive-function challenges — medical appointments "
        "and medication timing, insurance/benefits/legal admin, and protecting "
        "your own basic self-care from being crowded out."
    ),
    # A caregiver leans on getting-to-appointments-on-time (time_blindness) and on
    # starting dreaded admin — insurance calls, benefits forms (task_paralysis) —
    # and, unlike other contexts, on *self_care*: caregiver self-neglect is the
    # signature risk, so the pack switches the basic-needs checks on (see the
    # ``self_care`` default below, which arms them).
    modules=("time_blindness", "task_paralysis", "self_care"),
    # Caregiver-life todo buckets (also the keys the windows below shape):
    # ``medical`` (appointments, prescriptions, pharmacy), ``admin`` (insurance,
    # benefits, legal, paperwork), ``caregiving`` (day-to-day care tasks + errands
    # for the person you look after).
    categories=("medical", "admin", "caregiving"),
    # No new commitment kind yet — a dedicated ``care`` kind pairs with the
    # caregiver surface/situation-tools slice (see the module docstring), so it's
    # deferred rather than declared as dead vocabulary here.
    commitment_kinds=(),
    # Defaults, all absent-only (a user's own value always wins):
    # - keep medical/admin work inside the daytime hours those calls and clinics
    #   actually happen (the scheduler reads these ``todo_window:<category>`` keys);
    # - **arm self-care**: ``self_care=on`` turns on the meal + water checks so a
    #   caregiver pouring everything into someone else still gets the "have you
    #   eaten?" nudge — the whole reason the pack includes the module;
    # - **protect personal time**: the focus-balance guardrail
    #   (``prefrontal/focus_balance.py``) rolls out-of-home time up by life-sphere;
    #   a caregiver most loses *personal* time, so set a modest weekly aim for it
    #   (minutes of out-of-home trips/week) and light the gentle "light on personal
    #   this week" heads-up.
    coaching_defaults=MappingProxyType(
        {
            "todo_window:medical": "08:00-17:00",
            "todo_window:admin": "09:00-17:00",
            "self_care": "on",
            "focus_balance_nudge": "1",
            "focus_target:personal": "180",
        }
    ),
)

register(CAREGIVER_PACK)
