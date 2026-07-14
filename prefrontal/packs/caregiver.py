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

Deliberately declarative and small. The pack now carries its own *situation
tools* — the read-only, on-demand questions a caregiver asks, each a thin
composition of a shipped primitive, mirroring the Parent pack's set on the
caregiver's own three pressure points:

* **care run** — the leave-by for the care recipient's appointments (the
  departure engine narrowed to ``care`` commitments), the caregiver counterpart
  of the Parent pack's school run;
* **paperwork** — a one-step-at-a-time way into the dreaded admin pile
  (task decomposition over open ``admin`` todos), the caregiver counterpart of
  pack-the-bag; and
* **respite** — the self-neglect check that defines the risk: which of your own
  basics you've skipped today (self-care status) alongside the single thing that
  genuinely needs you (panic triage), so caring for someone else doesn't erase
  caring for yourself.

A caregiver *surface* (a care-recipient facts sheet, a shared-with-siblings care
log — the counterparts to the Parent pack's household sheet and ``/kids``) is the
remaining slice; the ``care`` commitment kind these tools read is already wired.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from prefrontal.packs.base import Pack, SituationTool
from prefrontal.packs.registry import register

if TYPE_CHECKING:
    from prefrontal.integrations import Generator
    from prefrontal.memory.store import MemoryStore

#: Cap on todos broken down in one paperwork checklist. The admin pile is exactly
#: the overwhelm this defuses, so it hands back a startable few — not the whole
#: backlog, which would re-create the freeze.
MAX_PAPERWORK_TODOS = 3

#: The caregiver ``category`` whose open todos the paperwork tool decomposes — the
#: insurance / benefits / legal / paperwork bucket (see :attr:`Pack.categories`).
_ADMIN_CATEGORY = "admin"

#: Friendly nouns for the self-care check keys, for the respite headline ("behind
#: on meals and water"). Keys mirror :data:`prefrontal.modules.self_care.CHECKS`.
_CHECK_LABELS = {
    "meal": "meals",
    "water": "water",
    "meds": "meds",
    "biobreak": "breaks",
    "winddown": "wind-down",
    "movement": "movement",
}


def _join(parts: list[str]) -> str:
    """Human list join: ``[a]`` → ``a``; ``[a, b]`` → ``a and b``; ``[a, b, c]`` →
    ``a, b, and c``. Used to phrase the skipped-basics list in the respite headline."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _care_run_situation(store: MemoryStore, *, client: Generator | None = None) -> dict[str, Any]:
    """When to leave for the care recipient's upcoming appointments — the care run.

    The caregiver counterpart of the Parent pack's school run: composes the
    departure primitive (:func:`plan_upcoming_departures`), the same engine behind
    the standing departure nudge, and narrows it to the caregiver's ``care``
    commitments — a cardiology follow-up, a dialysis run, a benefits-office visit
    for the person you look after. Read-only: it plans from the store's last *fresh*
    fix (falling back to each commitment's static lead) and never fires or records a
    nudge, so a caregiver can ask "when do I leave to get Mom there?" on demand
    without arming the reminder.

    Returns a ``headline`` (the most urgent leave-by phrased for a push, empty when
    nothing is pressing) plus every upcoming care departure with its leave-by. Fully
    deterministic — the ``client`` is accepted for the uniform tool contract and
    unused here.
    """
    from prefrontal.commitments import KIND_CARE
    from prefrontal.departure import (
        build_departure_message,
        next_departure,
        plan_upcoming_departures,
    )

    runs = [
        plan
        for plan in plan_upcoming_departures(store)
        if plan.commitment.get("kind") == KIND_CARE
    ]
    top = next_departure(runs)
    return {
        "tool": "care_run",
        "title": "Care run",
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


#: The Caregiver pack's care-run situation tool: the appointment leave-by.
CARE_RUN_TOOL = SituationTool(
    key="care_run",
    title="Care run",
    description="When to leave for the care recipient's upcoming appointments.",
    handler=_care_run_situation,
)


def _paperwork_situation(
    store: MemoryStore, *, client: Generator | None = None
) -> dict[str, Any]:
    """A one-step-at-a-time way into the admin pile — paperwork, decomposed.

    Insurance appeals, benefits forms, the legal packet: the caregiver's signature
    freeze is *starting* the dreaded admin, not doing it. This composes the
    task-paralysis initiation lever (:func:`prefrontal.todos.decompose_task`) over
    the caregiver's open ``admin`` todos — for each of the top
    :data:`MAX_PAPERWORK_TODOS` (already ordered priority then soonest deadline) it
    breaks the task into one tiny first step plus the remaining steps, so the pile
    becomes an ordered set of moves you can start on rather than a wall.

    Read-only: it only *plans* the checklists and never writes. Actively-delegated
    todos are excluded (a hand-off is off your plate). With a model ``client`` the
    steps are task-specific; with none they fall back to the deterministic
    first-step heuristic, exactly like every other decomposition surface.

    Returns a ``headline`` (the top task's first step, phrased for a push, empty
    when the admin pile is clear) plus a ``checklists`` entry per task.
    """
    from prefrontal.todos import decompose_task

    todos = [
        t
        for t in store.open_todos(exclude_delegated=True)
        if t.get("category") == _ADMIN_CATEGORY
    ][:MAX_PAPERWORK_TODOS]

    checklists = []
    for t in todos:
        title = t.get("title") or "the admin task"
        plan = decompose_task(title, client=client)
        checklists.append(
            {
                "todo_id": t.get("id"),
                "title": title,
                "first_step": plan.first_step,
                "steps": plan.steps,
                "source": plan.source,
            }
        )

    headline = ""
    if checklists:
        top = checklists[0]
        headline = f"Admin pile — start here: {top['first_step']}"
    return {
        "tool": "paperwork",
        "title": "Paperwork",
        "headline": headline,
        "checklists": checklists,
    }


#: The Caregiver pack's paperwork situation tool (see the handler).
PAPERWORK_TOOL = SituationTool(
    key="paperwork",
    title="Paperwork",
    description="A one-step-at-a-time way into the insurance/benefits/legal admin pile.",
    handler=_paperwork_situation,
)


def _respite_situation(
    store: MemoryStore, *, client: Generator | None = None
) -> dict[str, Any]:
    """The self-neglect check — what *you* skipped today, next to what needs you.

    The risk that defines caregiving is pouring everything into someone else until
    your own basics lapse, so this is the pack's distinctive tool. It composes two
    read-only primitives:

    * :func:`prefrontal.modules.self_care.self_care_status` for which of *your* own
      basics are behind today (the meal/water/… checks this pack arms), and
    * :func:`prefrontal.panic.build_panic` for the single calm next step and the
      count of what is *genuinely* pressing right now.

    Putting them side by side answers "am I running myself into the ground?" with
    the honest counterweight: the one thing that truly needs you, so stepping back
    for five minutes is a decision made against the real load, not guilt. Read-only,
    and fully deterministic — :func:`~prefrontal.panic.build_panic` is model-free
    (its first step uses the heuristic ``decompose_task`` internally, no ``client``),
    so the ``client`` here is accepted only for the uniform tool contract and unused.

    Returns the skipped basics, the panic first step + pressing count, whether the
    self-care checks are even armed, and a one-line ``headline`` for the push.
    """
    from prefrontal.config import get_settings
    from prefrontal.impact import utcnow
    from prefrontal.modules.self_care import self_care_status
    from prefrontal.panic import build_panic

    now = utcnow()
    status = self_care_status(store, now, get_settings().timezone)
    self_care_on = bool(status.get("enabled"))
    # "Behind on this right now" is exactly what ``overdue`` means (pace-aware for a
    # quota check, due-now for an open-ended one), so it's the honest "skipped today"
    # signal — and it's already false when a check is off, done, or before its start
    # hour. Only meaningful while the master switch is on.
    skipped = (
        [
            {"key": c["key"], "count": c["count"], "target": c["target"]}
            for c in status.get("checks", [])
            if c.get("enabled") and c.get("overdue")
        ]
        if self_care_on
        else []
    )

    plan = build_panic(store, now=now)
    pressing = plan.counts.get("pressing", 0)
    need = (
        f"the one thing that truly needs you: {plan.first_step_for}"
        if plan.first_step_for
        else "nothing's truly pressing right now"
    )

    if skipped:
        labels = _join([_CHECK_LABELS.get(s["key"], s["key"]) for s in skipped])
        headline = f"You're behind on {labels} today — and {need}. Take five for yourself."
    elif not self_care_on:
        # The checks aren't armed, so there's no "skipped" signal to give — still
        # surface the load counterweight and point at the switch.
        headline = f"Turn self-care checks on to track your own basics — meanwhile, {need}."
    else:
        headline = f"Basics covered today — and {need}."

    return {
        "tool": "respite",
        "title": "Respite check",
        "headline": headline,
        "self_care_on": self_care_on,
        "skipped": skipped,
        "pressing": pressing,
        "first_step": plan.first_step,
        "first_step_for": plan.first_step_for,
    }


#: The Caregiver pack's respite situation tool (see the handler).
RESPITE_TOOL = SituationTool(
    key="respite",
    title="Respite check",
    description="Your own basics skipped today, beside the one thing that needs you.",
    handler=_respite_situation,
)

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
    # ``self_care`` default below, which arms them). ``trip_tracking`` rides along
    # because the focus-balance guardrail (seeded below) lives entirely in that
    # module — it detects the out-of-home trips the "light on personal" nudge
    # measures and fires the nudge itself; without it the seeded target/flag would
    # be inert for a caregiver on an explicit PREFRONTAL_MODULES list.
    modules=("time_blindness", "task_paralysis", "self_care", "trip_tracking"),
    # Caregiver-life todo buckets (also the keys the windows below shape):
    # ``medical`` (appointments, prescriptions, pharmacy), ``admin`` (insurance,
    # benefits, legal, paperwork), ``caregiving`` (day-to-day care tasks + errands
    # for the person you look after).
    categories=("medical", "admin", "caregiving"),
    # The ``care`` commitment kind (``prefrontal.commitments.KIND_CARE``): an
    # appointment for the adult the caregiver looks after — the care-recipient
    # counterpart to a kid's ``child`` appointment. Now wired into the classifier
    # and treated as a real, attendable obligation.
    commitment_kinds=("care",),
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
    # The on-demand situations this pack answers from live data (read-only), each a
    # thin composition of a shipped primitive on a caregiver pressure point: the
    # care-run leave-by (departure engine over ``care`` commitments), the paperwork
    # decomposition (task decomposition over open ``admin`` todos), and the respite
    # self-neglect check (self-care status + panic triage).
    situations=(CARE_RUN_TOOL, PAPERWORK_TOOL, RESPITE_TOOL),
)

register(CAREGIVER_PACK)
