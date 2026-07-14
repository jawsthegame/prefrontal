"""Panic mode — a triage for when you're overwhelmed and frozen.

The morning briefing (:mod:`prefrontal.briefing`) is a calm overview of the whole
day. Panic mode is its opposite number: you feel buried, you can't tell what's
actually on fire versus what just *feels* loud, and you don't know where to put
your hands. This module answers three questions, in order:

1. **What is actually bearing down on me right now?** — a ruthlessly short,
   cross-context list: calendar things you're already late to leave for, todos
   past their deadline, urgent unread mail. Each is tagged with where it came
   from (which calendar, which inbox) so *work* and *home* pressures sit side by
   side instead of blurring together.
2. **What can I ignore?** — everything not on that list. Naming the finite set of
   real fires is itself the relief: the pile is smaller than the dread.
3. **How do I get moving?** — one concrete, tiny first step for the single most
   pressing thing, reusing the task-paralysis decomposition lever
   (:func:`prefrontal.todos.decompose_task`). Not the whole task — just the
   inertia-breaking first physical action.

Like the briefing and the profile summarizer, there are two layers: a
deterministic, model-free :func:`build_panic` / :func:`render_panic` (fully
testable), and an optional :func:`summarize_panic` that rewrites the rendered
digest as warm prose via the local Ollama model, falling back to the
deterministic text.

Pressures are sorted into three buckets — ``late`` (you're already behind),
``soon`` (bearing down within the hour or two), and ``piling_up`` (accumulating,
no hard clock) — and each carries a numeric ``score`` so the single worst thing
can be pulled out for the first step. The scores are coarse tiers (a commitment
you're late to leave for outranks an overdue todo outranks urgent mail), chosen
to be explainable rather than precise.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from prefrontal.clock import TS_FMT, end_of_local_day_utc, local_day_bounds
from prefrontal.clock import parse_ts as _parse_dt
from prefrontal.commitments import is_attendable
from prefrontal.config import get_settings
from prefrontal.impact import cascade_at_risk, cascade_impact, utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.todos import DEFAULT_MAX_FIRST_STEP_MINUTES, avoided_todos, decompose_task

if TYPE_CHECKING:
    from prefrontal.integrations import Generator

#: Commitments whose latest safe departure falls within this many minutes are
#: "bearing down soon"; earlier than that and they're not a panic item yet.
SOON_WINDOW_MINUTES = 120.0

#: Cap on how many items each bucket surfaces — panic mode must stay short, or it
#: becomes the pile it's meant to shrink.
MAX_PER_BUCKET = 5

#: System prompt for the optional LLM prose pass.
PANIC_SYSTEM_PROMPT = (
    "You are Prefrontal's panic-mode voice for someone with ADHD who is "
    "overwhelmed and frozen. You are given a structured triage of what is "
    "actually pressing right now. Rewrite it as a short, steadying, second-person "
    "message: open by grounding them (the list is finite), then give the single "
    "concrete first step verbatim, then name the other pressing items plainly and "
    "briefly. Do not add new tasks or invent items. Do not minimize real "
    "deadlines, but do not catastrophize either. Keep it tight — a few sentences. "
    "No preamble."
)


@dataclass(frozen=True)
class Pressure:
    """One thing bearing down, normalized across calendar / todos / mail.

    Attributes:
        bucket: ``late`` | ``soon`` | ``piling_up``.
        kind: ``commitment`` | ``todo`` | ``mail`` | ``blocker`` (the source domain).
        title: Short human label for the thing.
        when: Human phrasing of its timing (``"12 min late"``, ``"leave in 8 min"``,
            ``"2d overdue"``).
        source: Where it came from — a calendar label or an inbox account — so
            work vs. home context is visible. ``None`` when unknown.
        score: Coarse priority; higher is more pressing. Used to rank within a
            bucket and to pick the single item for the first step.
        commitment_id: The commitment's id, when ``kind == "commitment"``.
        todo_id: The todo's id, when ``kind == "todo"``.
    """

    bucket: str
    kind: str
    title: str
    when: str
    score: float
    source: str | None = None
    commitment_id: int | None = None
    todo_id: int | None = None
    blocker_id: int | None = None


@dataclass(frozen=True)
class PanicPlan:
    """A structured "here's what's actually on fire" triage.

    Attributes:
        date: The triage date (UTC, ``YYYY-MM-DD``).
        late: Things you're already behind on, worst first.
        soon: Things bearing down within the next hour or two, worst first.
        piling_up: Things accumulating with no hard clock, worst first.
        first_step: One concrete, tiny action to break the freeze (or ``None`` when
            nothing is pressing).
        first_step_for: The title of the item ``first_step`` addresses.
        counts: ``{"late", "soon", "piling_up", "pressing"}`` counts for the
            grounding line (``pressing`` = late + soon).
        headline: A single spoken/notification line — grounding + the first step —
            for a one-tap Shortcut or a proactive push.
        cascade: The knock-on chain — upcoming commitments that also topple because
            you're already behind (from :func:`prefrontal.impact.cascade_impact`
            seeded at now), worst chained first. Empty unless being late here puts
            two or more downstream commitments at risk.
    """

    date: str
    late: list[Pressure] = field(default_factory=list)
    soon: list[Pressure] = field(default_factory=list)
    piling_up: list[Pressure] = field(default_factory=list)
    first_step: str | None = None
    first_step_for: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    headline: str = ""
    cascade: list[dict[str, Any]] = field(default_factory=list)


# --- Timestamp parsing -------------------------------------------------------


def _parse_deadline(ts: Any) -> datetime | None:
    """Parse a todo deadline, which may be a full timestamp or a bare date.

    Todos created via :func:`prefrontal.todos.augment_todo` store a date-only
    deadline (``YYYY-MM-DD``); the endpoint accepts full ``YYYY-MM-DD HH:MM:SS``.
    A bare date is treated as end-of-day *in the user's local zone* (converted to
    UTC) so a todo due "today" isn't flagged overdue hours early in a western
    zone — nor until the local day is actually over.
    """
    dt = _parse_dt(ts)
    if dt is not None:
        return dt
    try:
        day = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return end_of_local_day_utc(day, get_settings().timezone)


# --- Human phrasing ----------------------------------------------------------


def _ago_phrase(minutes: float) -> str:
    """Compact "how long" phrasing for a positive minute count."""
    minutes = max(0.0, minutes)
    if minutes < 90:
        return f"{round(minutes)} min"
    hours = minutes / 60.0
    if hours < 36:
        return f"{round(hours)}h"
    return f"{round(hours / 24.0)}d"


# --- Collection --------------------------------------------------------------


def _commitment_pressures(
    store: MemoryStore, day_start: datetime, now: datetime, day_end: datetime
) -> list[Pressure]:
    """Calendar items you're already missing, should be leaving for, or about to.

    Scans the whole of today (not just what's still upcoming) so a hard meeting
    that has *already started* — the "oh no, I'm missing standup" case — surfaces
    too. Excludes ``fyi`` commitments (somewhere someone else will be, not your
    obligation) and anything that has already ended.
    """
    fmt = TS_FMT
    out: list[Pressure] = []
    for c in store.commitments_between(day_start.strftime(fmt), day_end.strftime(fmt)):
        if c.get("kind") == "fyi":
            continue
        start = _parse_dt(c.get("start_at"))
        if start is None:
            continue
        end = _parse_dt(c.get("end_at"))
        if end is not None and end < now:
            continue  # already over — nothing to panic about
        source = c.get("calendar")
        cid = c.get("id")
        hard = c.get("hardness") == "hard"
        lead = float(c.get("lead_minutes") or 0.0)
        latest_departure = start - timedelta(minutes=lead)
        mins_to_leave = (latest_departure - now).total_seconds() / 60.0
        if now >= start:
            # It has started and hasn't ended. Only a hard thing is a fire —
            # a soft/optional block you're "in" isn't something you're behind on.
            if not hard:
                continue
            out.append(
                Pressure(
                    bucket="late",
                    kind="commitment",
                    title=f"Get to {c['title']}",
                    when=f"started {_ago_phrase((now - start).total_seconds() / 60.0)} ago",
                    score=1100.0,
                    source=source,
                    commitment_id=cid,
                )
            )
        elif mins_to_leave < 0:
            out.append(
                Pressure(
                    bucket="late",
                    kind="commitment",
                    title=f"Leave for {c['title']}",
                    when=f"{_ago_phrase(-mins_to_leave)} past when you should go",
                    score=1000.0 + min(-mins_to_leave, 240.0),
                    source=source,
                    commitment_id=cid,
                )
            )
        elif mins_to_leave <= SOON_WINDOW_MINUTES:
            out.append(
                Pressure(
                    bucket="soon",
                    kind="commitment",
                    title=f"Leave for {c['title']}",
                    when=f"leave in {_ago_phrase(mins_to_leave)}",
                    score=700.0 + (SOON_WINDOW_MINUTES - mins_to_leave),
                    source=source,
                    commitment_id=cid,
                )
            )
    return out


def _todo_pressures(
    store: MemoryStore, now: datetime, day_end: datetime
) -> tuple[list[Pressure], dict[int, str]]:
    """Overdue / due-today / urgent / avoided todos, plus their inbox sources.

    Returns the pressures and a ``{todo_id: account}`` map (todos that came from
    mail) so the caller can label each with its originating inbox.
    """
    todos = store.open_todos(exclude_delegated=True)
    ids = [t["id"] for t in todos if t.get("id") is not None]
    accounts = store.mail_accounts_for_todos(ids)

    out: list[Pressure] = []
    seen: set[int] = set()
    for t in todos:
        tid = t.get("id")
        deadline = _parse_deadline(t.get("deadline"))
        priority = int(t.get("priority") or 1)
        src = accounts.get(tid)
        if deadline is not None and deadline < now:
            overdue_min = (now - deadline).total_seconds() / 60.0
            out.append(
                Pressure(
                    bucket="late",
                    kind="todo",
                    title=t["title"],
                    when=f"{_ago_phrase(overdue_min)} overdue",
                    # Overdue todos live in the [800, 1000) band — strictly below
                    # the commitment tiers (a late-to-leave commitment is 1000+, a
                    # hard one you've started is 1100), so the coarse-tier contract
                    # "a commitment you're late for outranks an overdue todo" holds
                    # even for a very stale, high-priority todo. The overdue-hours
                    # and priority terms are scaled to stay under that ceiling.
                    score=800.0 + min(overdue_min / 60.0, 240.0) * 0.5 + priority * 20.0,
                    source=src,
                    todo_id=tid,
                )
            )
            seen.add(tid)
        elif deadline is not None and deadline <= day_end:
            out.append(
                Pressure(
                    bucket="soon",
                    kind="todo",
                    title=t["title"],
                    when="due today",
                    score=500.0 + priority * 30.0,
                    source=src,
                    todo_id=tid,
                )
            )
            seen.add(tid)
        elif priority >= 3:
            out.append(
                Pressure(
                    bucket="soon",
                    kind="todo",
                    title=t["title"],
                    when="marked urgent",
                    score=480.0,
                    source=src,
                    todo_id=tid,
                )
            )
            seen.add(tid)

    # Avoided: the important things you keep not doing (no hard clock, so they
    # never surface above — but a panic reset is exactly when to name them).
    for a in avoided_todos(todos, now):
        tid = a["todo"].get("id")
        if tid in seen:
            continue
        out.append(
            Pressure(
                bucket="piling_up",
                kind="todo",
                title=a["todo"]["title"],
                when=f"open {a['days_open']:g}d, kept skipping",
                score=100.0 + a["score"],
                source=accounts.get(tid),
                todo_id=tid,
            )
        )
        seen.add(tid)
    return out, accounts


def _mail_pressures(store: MemoryStore) -> list[Pressure]:
    """Unread mail flagged as needing action, split by urgency."""
    out: list[Pressure] = []
    for m in store.mail_needing_action():
        urgency = (m.get("urgency") or "").lower()
        who = m.get("sender_name") or m.get("sender_email") or "someone"
        subject = m.get("subject") or "(no subject)"
        title = f"Reply to {who}: {subject}"
        if urgency == "urgent":
            out.append(
                Pressure(
                    bucket="soon",
                    kind="mail",
                    title=title,
                    when="urgent email",
                    score=460.0,
                    source=m.get("account"),
                )
            )
        elif urgency == "high":
            out.append(
                Pressure(
                    bucket="piling_up",
                    kind="mail",
                    title=title,
                    when="needs a reply",
                    score=90.0,
                    source=m.get("account"),
                )
            )
    return out


def _blocker_pressures(
    store: MemoryStore, now: datetime, day_end: datetime
) -> list[Pressure]:
    """People who are blocked on *you* — the ball's in your court.

    A blocker is the counterweight to shiny-object syndrome: in a panic reset it's
    exactly the thing to name, because someone else's day is stalled until you
    move. Bucketed like todos — past its "needs it by" is ``late``, due-today or
    urgent is ``soon``, otherwise it's ``piling_up`` — but scored a notch above the
    equivalent todo, since a person waiting outranks your own smoulder. The score
    stays under the commitment tiers (1000+), so "a commitment you're late for
    outranks everything else" still holds.
    """
    out: list[Pressure] = []
    for b in store.open_blockers():
        person = (b.get("person") or "Someone").strip() or "Someone"
        what = (b.get("what") or "something").strip()
        priority = int(b.get("priority") or 1)
        bid = b.get("id")
        since = _parse_dt(b.get("blocking_since"))
        waited_min = (now - since).total_seconds() / 60.0 if since is not None else 0.0
        waited = f"waiting {_ago_phrase(waited_min)}"
        title = f"{person} is waiting on you: {what}"
        deadline = _parse_deadline(b.get("deadline"))
        if deadline is not None and deadline < now:
            overdue_min = (now - deadline).total_seconds() / 60.0
            out.append(
                Pressure(
                    bucket="late",
                    kind="blocker",
                    title=title,
                    when=f"{_ago_phrase(overdue_min)} past due · {waited}",
                    # In the [800, 1000) band with overdue todos but a notch above
                    # (850 base vs 800): a person past-due on you edges out your own
                    # overdue todo. Still strictly below the commitment tiers.
                    score=850.0 + min(overdue_min / 60.0, 240.0) * 0.5 + priority * 20.0,
                    blocker_id=bid,
                )
            )
        elif deadline is not None and deadline <= day_end:
            out.append(
                Pressure(
                    bucket="soon",
                    kind="blocker",
                    title=title,
                    when=f"needed today · {waited}",
                    score=520.0 + priority * 30.0,
                    blocker_id=bid,
                )
            )
        elif priority >= 3:
            out.append(
                Pressure(
                    bucket="soon",
                    kind="blocker",
                    title=title,
                    when=f"marked urgent · {waited}",
                    score=500.0,
                    blocker_id=bid,
                )
            )
        else:
            out.append(
                Pressure(
                    bucket="piling_up",
                    kind="blocker",
                    title=title,
                    when=waited,
                    # Above avoided todos (100+) and high mail (90): the whole point
                    # is that someone waiting on you outranks your own back-burner.
                    score=150.0 + min(waited_min / 1440.0, 30.0) + priority * 10.0,
                    blocker_id=bid,
                )
            )
    return out


def _cascade_chain(
    store: MemoryStore, day_end: datetime, now: datetime
) -> list[dict[str, Any]]:
    """The knock-on chain: upcoming commitments toppled by being behind *now*.

    Seeds :func:`prefrontal.impact.cascade_impact` at ``now`` over today's
    not-yet-started self commitments, so it flags only those you're already too
    late to leave for and the downstream ones their overrun pushes. Returns the
    at-risk links (schedule order) only when there are **two or more** — a lone
    late item is already named in the ``late`` bucket, so the cascade earns its
    space only when it reveals a genuine domino the buckets don't show.
    """
    fmt = TS_FMT
    upcoming = [
        c
        for c in store.commitments_between(now.strftime(fmt), day_end.strftime(fmt))
        if is_attendable(c) and _parse_dt(c.get("start_at")) is not None
    ]
    # Travel-aware leads from the phone's last known fix, so a leg you can't
    # actually drive in the flat buffer is flagged (empty → static leads).
    from prefrontal.departure import departure_kwargs, travel_leads

    loc = store.get_location()
    dep = departure_kwargs(store)
    leads = travel_leads(
        upcoming,
        loc["lat"] if loc else None,
        loc["lon"] if loc else None,
        bias=dep["bias"], speed_kmh=dep["speed_kmh"],
        road_factor=dep["road_factor"], prep_minutes=dep["prep_minutes"],
        pad_fraction=dep["pad_fraction"],
    )
    risky = cascade_at_risk(cascade_impact(now, upcoming, lead_override=leads))
    if len(risky) < 2:
        return []
    return [
        {
            "title": i.commitment["title"],
            "start_at": i.commitment["start_at"],
            "projected_start": i.projected_start,
            "delay_minutes": i.delay_minutes,
            "slack_minutes": i.slack_minutes,
            "caused_by": i.caused_by,
            "hardness": i.commitment.get("hardness"),
        }
        for i in risky
    ]


# --- First step (the inertia-breaker) ----------------------------------------


def _first_step_for(top: Pressure, *, max_minutes: float) -> str:
    """A concrete, tiny first action for the single most pressing item.

    For a todo, defers to the task-paralysis decomposition heuristic (open the
    file, find the number, write one ugly line). For a commitment or an email the
    first move is obvious and physical, so it's phrased directly.
    """
    if top.kind == "commitment":
        return (
            "Stop what you're doing and move toward the door right now — grab "
            "keys, phone, wallet, and go. Sort out everything else on the way."
        )
    if top.kind == "mail":
        return (
            "Open it and send a one-line holding reply — “got this, will come "
            "back to you by <time>”. That clears the alarm; the real answer can "
            "wait."
        )
    if top.kind == "blocker":
        return (
            "Send them a two-line update right now — “here's where this stands, "
            "you'll have it by <when>”. Clearing the wait on their side is the "
            "unblock; the finished work can follow."
        )
    # todo — break the freeze with the smallest physical action.
    return decompose_task(top.title, max_first_minutes=max_minutes).first_step


# --- Build / render ----------------------------------------------------------


def build_panic(store: MemoryStore, now: Any | None = None) -> PanicPlan:
    """Assemble the structured panic triage from memory (deterministic, model-free).

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`PanicPlan`.
    """
    now = now or utcnow()
    # "Today" is the user's *local* day: an evening panic check (after the UTC
    # rollover for a western-hemisphere user) must still reason about tonight's
    # commitments, not tomorrow-UTC's. day_end is the next local midnight.
    day_start, day_end = local_day_bounds(now, get_settings().timezone)

    pressures: list[Pressure] = []
    pressures.extend(_commitment_pressures(store, day_start, now, day_end))
    todo_pressures, _ = _todo_pressures(store, now, day_end)
    pressures.extend(todo_pressures)
    pressures.extend(_mail_pressures(store))
    pressures.extend(_blocker_pressures(store, now, day_end))

    def bucket(name: str) -> list[Pressure]:
        items = sorted(
            (p for p in pressures if p.bucket == name), key=lambda p: -p.score
        )
        return items[:MAX_PER_BUCKET]

    late = bucket("late")
    soon = bucket("soon")
    piling_up = bucket("piling_up")

    # The one thing: the highest-scoring item across late + soon (the things with
    # a real clock). Avoided/piling-up items never become the forced first step —
    # in a panic you fight the fire, not the smoulder.
    step_from = late + soon
    first_step = first_step_for = None
    if step_from:
        try:
            max_minutes = float(
                store.get_state("max_first_step_minutes")
                or DEFAULT_MAX_FIRST_STEP_MINUTES
            )
        except (TypeError, ValueError):
            max_minutes = DEFAULT_MAX_FIRST_STEP_MINUTES
        top = max(step_from, key=lambda p: p.score)
        first_step = _first_step_for(top, max_minutes=max_minutes)
        first_step_for = top.title

    counts = {
        "late": len(late),
        "soon": len(soon),
        "piling_up": len(piling_up),
        "pressing": len(late) + len(soon),
    }
    plan = PanicPlan(
        date=now.strftime("%Y-%m-%d"),
        late=late,
        soon=soon,
        piling_up=piling_up,
        first_step=first_step,
        first_step_for=first_step_for,
        counts=counts,
        cascade=_cascade_chain(store, day_end, now),
    )
    return replace(plan, headline=_headline(plan))


def _headline(plan: PanicPlan) -> str:
    """One spoken/notification line: grounding + the first step (for one-tap use)."""
    pressing = plan.counts.get("pressing", 0)
    if not pressing and not plan.piling_up:
        return "Nothing's actually on fire right now — take a breath. The board's empty."
    if plan.first_step:
        if pressing == 1:
            lead = "1 thing needs you right now."
        elif pressing:
            lead = f"{pressing} things need you right now."
        else:
            lead = "Nothing has a hard clock right now."
        return f"{lead} Start here: {plan.first_step}"
    # Only piling-up items — no forced first step, just a gentle pointer.
    top = plan.piling_up[0].title
    return (
        f"Nothing's urgent, but a few things are piling up — like {top}. "
        "Pick one when you're ready."
    )


def _render_item(p: Pressure) -> str:
    """Render one pressure as a Markdown list line."""
    src = f" · {p.source}" if p.source else ""
    return f"- **{p.when}** — {p.title}{src}"


def render_panic(plan: PanicPlan) -> str:
    """Render a :class:`PanicPlan` as steadying Markdown.

    Leads with a grounding line and the single first step, then lists the pressing
    buckets. When nothing is pressing, it says so plainly (that's the reassurance).

    Args:
        plan: The structured triage.

    Returns:
        A Markdown string suitable for printing or delivery.
    """
    lines = ["# Panic mode", ""]
    pressing = plan.counts.get("pressing", 0)

    if pressing == 0 and not plan.piling_up:
        lines.append(
            "**Nothing is actually on fire.** No hard deadlines are past, nothing "
            "needs you in the next couple of hours, and your inbox is clear of "
            "urgent asks. The overwhelm is real, but the board is empty — take a "
            "breath, then pick one small thing you *want* to move."
        )
        return "\n".join(lines).rstrip() + "\n"

    # Grounding.
    if pressing == 0:
        lines.append(
            "**Breathe — nothing has a hard clock right now.** A few things are "
            "piling up, but you're not late on any of them. Do one below and the "
            "pile shrinks."
        )
    else:
        noun = "thing" if pressing == 1 else "things"
        lines.append(
            f"**Breathe. {pressing} {noun} actually need you right now** — that's "
            "the whole list. Everything else can wait until you've done these."
        )
    lines.append("")

    # The one thing.
    if plan.first_step:
        lines.append("**▶ Start here:**")
        lines.append(f"{plan.first_step}")
        if plan.first_step_for:
            lines.append(f"_(that gets “{plan.first_step_for}” moving)_")
        lines.append("")

    if plan.late:
        lines.append(f"**🔴 Already behind ({len(plan.late)}):**")
        lines.extend(_render_item(p) for p in plan.late)
        lines.append("")

    if plan.soon:
        lines.append(f"**🟠 Bearing down soon ({len(plan.soon)}):**")
        lines.extend(_render_item(p) for p in plan.soon)
        lines.append("")

    # Knock-on: being late now doesn't stay local — name the downstream chain so
    # the true cost of the current slip is visible, not just the first fire.
    if plan.cascade:
        shown = plan.cascade[:3]
        chain = " → ".join(
            f"{c['title']} (~{round(-c['slack_minutes'])}m late)" for c in shown
        )
        more = "" if len(plan.cascade) <= 3 else f" +{len(plan.cascade) - 3} more"
        lines.append(f"**⚠️ Knock-on:** running behind here also topples {chain}{more}.")
        lines.append("")

    if plan.piling_up:
        lines.append(f"**🟡 Piling up — no clock, but don't forget ({len(plan.piling_up)}):**")
        lines.extend(_render_item(p) for p in plan.piling_up)
        lines.append("")

    lines.append("_Do the one thing at the top, then come back and look again._")
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class PanicResult:
    """Result of :func:`summarize_panic`."""

    text: str
    source: str  # "llm" | "heuristic"
    model: str | None = None


def summarize_panic(
    store: MemoryStore,
    *,
    client: Generator | None = None,
    now: Any | None = None,
    fallback: bool = True,
) -> PanicResult:
    """Render the panic triage and optionally rewrite it as prose via the model.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        client: A model client (defaults to the one the ``panic`` agent resolves
            to via :class:`~prefrontal.integrations.provider.ProviderResolver`).
        now: Optional naive-UTC "now".
        fallback: Fall back to the deterministic digest on model failure.

    Returns:
        A :class:`PanicResult`.

    Raises:
        prefrontal.integrations.base.ProviderError: If the model fails and
            ``fallback`` is ``False``.
    """
    from prefrontal.integrations.summarize import summarize_or_fallback

    rendered = render_panic(build_panic(store, now=now))
    summary = summarize_or_fallback(
        rendered,
        system=PANIC_SYSTEM_PROMPT,
        agent="panic",
        client=client,
        fallback=fallback,
    )
    return PanicResult(text=summary.text, source=summary.source, model=summary.model)


# --- Proactive alerting ------------------------------------------------------
#
# Panic mode is primarily on-demand, but the worst overwhelm spikes are exactly
# the moments you *don't* think to open anything. A poller (n8n) can hit
# ``POST /webhooks/panic/check`` on a cadence; these helpers decide whether the
# moment warrants an unprompted nudge. The bar is deliberately high — a single
# late item is normal life, not a crisis — and the endpoint edge-triggers on the
# level (like the departure reminder's signature) so it fires once when you tip
# into overwhelm, not on every poll.

#: Default pressing-count at/above which (with something already late) it alerts.
DEFAULT_ALERT_MIN_PRESSING = 3
#: Default minutes to stay quiet after an alert, even across a new spike edge.
DEFAULT_ALERT_COOLDOWN_MINUTES = 180.0


def overwhelm_level(plan: PanicPlan, *, min_pressing: int = DEFAULT_ALERT_MIN_PRESSING) -> str:
    """Classify a plan as ``"overwhelmed"`` or ``"calm"`` for proactive alerting.

    Overwhelmed when either two or more things are already late, or something is
    late *and* the total pressing count has reached ``min_pressing``. A lone
    upcoming item — or even one late item on an otherwise clear plate — stays
    ``calm``: the point is to catch a genuine pile-up, not to nag.
    """
    late = plan.counts.get("late", 0)
    pressing = plan.counts.get("pressing", 0)
    if late >= 2 or (late >= 1 and pressing >= min_pressing):
        return "overwhelmed"
    return "calm"


def panic_alert_message(plan: PanicPlan, *, name: str | None = None) -> str:
    """A short, kind push line for a proactive overwhelm alert.

    Names the shape of the pile-up (late / soon) and leads straight into the
    single first step, so the notification itself is actionable.
    """
    opener = f"Hey {name} — " if name else "Heads up — "
    late = plan.counts.get("late", 0)
    soon = plan.counts.get("soon", 0)
    parts = []
    if late:
        parts.append(f"{late} already late")
    if soon:
        parts.append(f"{soon} bearing down soon")
    shape = " and ".join(parts) if parts else "a few things"
    tail = f" Start here: {plan.first_step}" if plan.first_step else ""
    return f"{opener}things are stacking up — {shape}. Breathe; it's a short list.{tail}"


# --- First-step outcome capture ---------------------------------------------
# Did surfacing one tiny first step during overwhelm actually get it done? A
# fired nudge logs a *pending* "panic" episode; a one-tap "Did it" resolves it to
# success, and an unanswered one is swept to a miss so the learning loop sees the
# steps that DIDN'T happen too — otherwise the drift signal would be success-only.

#: Episode type for overwhelm first-step outcomes (also in EPISODE_TYPES).
PANIC_EPISODE_TYPE = "panic"

#: Default minutes to wait for a "Did it" tap before counting the step a miss.
DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES = 180.0


def record_panic_step_sent(
    store: MemoryStore, plan: PanicPlan, *, now: datetime | None = None
) -> int:
    """Log a *pending* panic episode for a fired nudge's first step; return its id.

    The id is what the one-tap "Did it" button signs against
    (:func:`resolve_panic_step`); until it resolves the episode has a NULL
    ``outcome`` and is invisible to the drift pass. Caller should only invoke this
    when a resolvable button will actually be delivered (a public origin + signing
    key), so an unresolvable nudge never becomes a phantom miss.
    """
    when = (now or utcnow()).strftime(TS_FMT)
    return store.log_episode(
        PANIC_EPISODE_TYPE,
        acknowledged=False,
        context=f"overwhelm first step: {plan.first_step_for or '?'}",
        notes=plan.first_step,
        timestamp=when,
    )


def resolve_panic_step(store: MemoryStore, episode_id: int) -> bool:
    """Mark a pending panic first-step episode done (a one-tap "Did it"); True if it changed."""
    return store.set_episode_outcome(episode_id, outcome="success", acknowledged=True)


def sweep_pending_panic_steps(
    store: MemoryStore,
    now: datetime,
    *,
    window_minutes: float = DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES,
) -> int:
    """Sweep panic first-step nudges left unanswered past the window into a miss.

    Balances the drift signal: without this only taps ("Did it") would ever be
    recorded, so ``panic`` drift would read as always-on-track. Returns the count
    swept. Mirrors :func:`prefrontal.coaching.sweep_stale_nudges`.
    """
    cutoff = (now - timedelta(minutes=window_minutes)).strftime(TS_FMT)
    stale = store.pending_episodes(PANIC_EPISODE_TYPE, before=cutoff)
    for ep in stale:
        store.set_episode_outcome(ep["id"], outcome="miss", acknowledged=False)
    return len(stale)


@dataclass(frozen=True)
class PanicCheck:
    """The proactive-panic decision (the model-free core of ``/webhooks/panic/check``).

    Attributes:
        fire: Whether an overwhelm nudge should be delivered now.
        level: ``calm`` | ``overwhelmed`` (:func:`overwhelm_level`).
        message: The nudge text when ``fire`` (empty otherwise).
        first_step: The single concrete first step (for display / the ack episode).
        counts: The plan's bucket counts.
        headline: The plan's one-line headline.
        step_id: The pending ``panic`` episode id recorded for a "Did it" ack, or
            ``None`` when not firing or acks aren't deliverable (``ackable=False``).
    """

    fire: bool
    level: str
    message: str
    first_step: str | None
    counts: dict[str, int]
    headline: str
    step_id: int | None = None


def evaluate_panic_check(
    store: MemoryStore,
    *,
    now: datetime | None = None,
    quiet_hours: bool = False,
    ackable: bool = False,
) -> PanicCheck:
    """Decide whether to fire a proactive overwhelm nudge, and record it if so.

    The shared decision behind ``POST /webhooks/panic/check`` and the native
    ``prefrontal coach --deliver`` tick, so both nudge identically without n8n.
    Edge-triggers on the overwhelm ``level`` (fires once per calm→overwhelmed
    transition, not every poll), **defers** — never drops — an edge that lands in
    quiet hours (``quiet_hours=True`` returns without advancing ``last_panic_level``
    so the first poll back in responsive hours still fires), and honors a cooldown
    floor. Always sweeps unanswered first-step nudges into a miss first (so drift
    isn't all "Did it" taps). When it fires and ``ackable`` (a signed one-tap
    button is deliverable), records a pending ``panic`` episode and returns its
    ``step_id`` so the caller can attach the "Did it" button; the caller builds the
    actual action buttons (they need the public origin / signing key / handle).
    """
    now = now or utcnow()
    plan = build_panic(store, now=now)

    # Sweep earlier unanswered first-step nudges regardless of firing (calm polls
    # and quiet-hours deferrals included), so the learning loop sees the steps that
    # didn't happen, not only the taps.
    sweep_pending_panic_steps(
        store,
        now,
        window_minutes=store.get_float(
            "panic_step_ack_window_minutes", DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES
        ),
    )

    try:
        min_pressing = int(
            store.get_state("panic_alert_min_pressing") or DEFAULT_ALERT_MIN_PRESSING
        )
    except (TypeError, ValueError):
        min_pressing = DEFAULT_ALERT_MIN_PRESSING
    cooldown = store.get_float("panic_alert_cooldown_minutes", DEFAULT_ALERT_COOLDOWN_MINUTES)

    level = overwhelm_level(plan, min_pressing=min_pressing)
    prev = store.get_state("last_panic_level", "calm")
    fire = level == "overwhelmed" and prev != "overwhelmed"

    # Quiet-hours: defer, don't drop — leave last_panic_level untouched so the edge
    # survives to the next responsive-hours poll.
    if fire and quiet_hours:
        return PanicCheck(False, level, "", plan.first_step, plan.counts, plan.headline)

    # Cooldown floor: even on a fresh edge, stay quiet if we alerted recently.
    if fire:
        last_at = _parse_dt(store.get_state("last_panic_alert_at"))
        if last_at is not None and (now - last_at).total_seconds() / 60.0 < cooldown:
            fire = False

    store.set_state("last_panic_level", level, source="inferred")
    if not fire:
        return PanicCheck(False, level, "", plan.first_step, plan.counts, plan.headline)

    name = (store.get_state("user_name") or "").strip() or None
    message = panic_alert_message(plan, name=name)
    step_id = None
    if plan.first_step and ackable:
        step_id = record_panic_step_sent(store, plan, now=now)
    store.set_state("last_panic_alert_at", now.strftime(TS_FMT), source="inferred")
    return PanicCheck(
        True, level, message, plan.first_step, plan.counts, plan.headline, step_id
    )
