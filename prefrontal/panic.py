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

from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.todos import DEFAULT_MAX_FIRST_STEP_MINUTES, avoided_todos, decompose_task

if TYPE_CHECKING:
    from prefrontal.integrations.ollama import OllamaClient

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
        kind: ``commitment`` | ``todo`` | ``mail`` (the source domain).
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
    """

    date: str
    late: list[Pressure] = field(default_factory=list)
    soon: list[Pressure] = field(default_factory=list)
    piling_up: list[Pressure] = field(default_factory=list)
    first_step: str | None = None
    first_step_for: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    headline: str = ""


# --- Timestamp parsing -------------------------------------------------------


def _parse_dt(ts: Any) -> datetime | None:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` (naive UTC) timestamp, or ``None``."""
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _parse_deadline(ts: Any) -> datetime | None:
    """Parse a todo deadline, which may be a full timestamp or a bare date.

    Todos created via :func:`prefrontal.todos.augment_todo` store a date-only
    deadline (``YYYY-MM-DD``); the endpoint accepts full ``YYYY-MM-DD HH:MM:SS``.
    A bare date is treated as end-of-day so a todo due "today" isn't flagged
    overdue until the day is actually over.
    """
    dt = _parse_dt(ts)
    if dt is not None:
        return dt
    try:
        day = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return day.replace(hour=23, minute=59, second=59)


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
    fmt = "%Y-%m-%d %H:%M:%S"
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
    todos = store.open_todos()
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
                    score=800.0 + min(overdue_min / 60.0, 240.0) + priority * 30.0,
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
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    pressures: list[Pressure] = []
    pressures.extend(_commitment_pressures(store, day_start, now, day_end))
    todo_pressures, _ = _todo_pressures(store, now, day_end)
    pressures.extend(todo_pressures)
    pressures.extend(_mail_pressures(store))

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
    client: OllamaClient | None = None,
    now: Any | None = None,
    fallback: bool = True,
) -> PanicResult:
    """Render the panic triage and optionally rewrite it as prose via Ollama.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        client: An Ollama client (defaults to one built from settings).
        now: Optional naive-UTC "now".
        fallback: Fall back to the deterministic digest on model failure.

    Returns:
        A :class:`PanicResult`.

    Raises:
        prefrontal.integrations.ollama.OllamaError: If the model fails and
            ``fallback`` is ``False``.
    """
    from prefrontal.integrations.ollama import OllamaClient, OllamaError

    rendered = render_panic(build_panic(store, now=now))
    client = client or OllamaClient.from_settings()
    try:
        prose = client.generate(rendered, system=PANIC_SYSTEM_PROMPT).strip()
    except OllamaError:
        if not fallback:
            raise
        return PanicResult(text=rendered, source="heuristic")
    if not prose:
        if not fallback:
            raise OllamaError("Ollama returned an empty panic summary.")
        return PanicResult(text=rendered, source="heuristic")
    return PanicResult(text=prose, source="llm", model=client.model)


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
