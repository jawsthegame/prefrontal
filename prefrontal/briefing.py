"""Morning briefing — a daily digest assembled from everything Prefrontal knows.

The README promises "a daily digest calibrated to your preferences: what needs
attention today, what went cold this week, what's on the calendar." This module
delivers it by synthesizing the pieces already built:

- **Today's commitments** — from the synced calendar (`commitments`).
- **Conflicts** — double-bookings among upcoming commitments.
- **What slipped** — recent `miss` episodes (late departures, abandoned outings,
  ignored reminders) over the past week.
- **Coaching note** — the learned time bias and responsive hours, so the briefing
  reminds you of your own patterns.

Like the profile summarizer, there are two layers: a deterministic
:func:`build_briefing` / :func:`render_briefing` (testable, no model), and an
optional :func:`summarize_briefing` that runs the rendered digest through the
local Ollama model for friendly prose, falling back to the deterministic text.
The ``preferred_briefing_format`` coaching key (``short``/``long``) controls how
much detail the rendering includes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from prefrontal.clock import TS_FMT
from prefrontal.commitments import find_conflicts
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
from prefrontal.memory.patterns import task_bias_resolver
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import free_windows, suggest_for_windows, window_config_for
from prefrontal.todos import avoided_todos

#: Default available-hours band (UTC hours) for fitting todos into the day.
DEFAULT_DAY_START_HOUR = 8
DEFAULT_DAY_END_HOUR = 20

if TYPE_CHECKING:
    from prefrontal.integrations import Generator

#: System prompt for the LLM briefing pass.
BRIEFING_SYSTEM_PROMPT = (
    "You are Prefrontal's morning-briefing voice for someone with ADHD. You are "
    "given a structured daily digest. Rewrite it as a brief, warm, second-person "
    "briefing: lead with what needs attention today, call out any double-booking "
    "or at-risk item plainly, and end with one concrete suggestion. Keep it to a "
    "few sentences. Use the numbers as given; do not invent events. No preamble."
)

#: How many days back "what slipped" looks.
SLIP_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Briefing:
    """Structured morning briefing.

    Attributes:
        date: The briefing date (UTC, ``YYYY-MM-DD``).
        format: ``short`` or ``long`` (from coaching state).
        today: Today's commitments (dicts), soonest first.
        conflicts: Double-booking pairs among upcoming commitments.
        slips: Count of recent ``miss`` episodes by ``episode_type``.
        coaching: Selected coaching values surfaced in the briefing.
        spare: Free windows in the rest of the day, each with a suggested todo.
        encouragement: Optional closing encouragement line (spec §6.2) — set when
            the ``encouragement`` layer is on and the day warrants a note; ``None``
            leaves the briefing's usual time-bias reminder in place.
    """

    date: str
    format: str
    today: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    slips: dict[str, int] = field(default_factory=dict)
    coaching: dict[str, str] = field(default_factory=dict)
    spare: list[dict[str, Any]] = field(default_factory=list)
    avoided: list[dict[str, Any]] = field(default_factory=list)
    encouragement: str | None = None


def build_briefing(store: MemoryStore, now: Any | None = None) -> Briefing:
    """Assemble the structured briefing from memory.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`Briefing`.
    """
    now = now or utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    fmt = TS_FMT

    today = store.commitments_between(day_start.strftime(fmt), day_end.strftime(fmt))

    # Double-bookings across today's schedule (regardless of the current hour),
    # which is what a morning overview cares about.
    conflicts = [
        {
            "a": c.a["title"],
            "b": c.b["title"],
            "overlap_minutes": c.overlap_minutes,
        }
        for c in find_conflicts(today)
    ]

    since = (now - timedelta(days=SLIP_WINDOW_DAYS)).strftime(fmt)
    slip_counter: Counter[str] = Counter()
    for e in store.episodes_since(since):
        if e.get("outcome") == "miss":
            slip_counter[e["episode_type"]] += 1

    state = store.all_state()
    coaching = {
        k: state[k]["value"]
        for k in (
            "time_estimation_bias",
            "responsive_hours_start",
            "responsive_hours_end",
            "user_name",
        )
        if k in state
    }
    fmt_pref = state.get("preferred_briefing_format", {}).get("value", "short")

    # Spare time: free windows in the rest of the day, each with a fitting todo.
    spare: list[dict[str, Any]] = []
    todos = store.open_todos()
    if todos:
        try:
            bias = float(state.get("time_estimation_bias", {}).get("value", 1.0))
        except (TypeError, ValueError):
            bias = 1.0
        band_start = max(now, day_start.replace(hour=DEFAULT_DAY_START_HOUR))
        band_end = day_start.replace(hour=DEFAULT_DAY_END_HOUR)
        if band_end > band_start:
            # Only propose a todo into a window its category/source/off-zone
            # policy actually allows (e.g. no focus work at 8pm, nothing overnight).
            settings = get_settings()
            window_config = window_config_for(settings, store)
            windows = free_windows(today, band_start, band_end)
            # Size each spare window by *its own* time of day and each candidate
            # by its own energy/category (§5): the 9am gap uses the morning bias,
            # a heavy todo there is padded by its energy bias, etc.
            for s in suggest_for_windows(
                windows,
                todos,
                bias,
                config=window_config,
                tz=settings.timezone,
                resolver_for_hour=lambda hour: task_bias_resolver(store, local_hour=hour),
            ):
                pick = s["suggestion"]
                spare.append(
                    {
                        "start": s["window"].start,
                        "minutes": s["window"].minutes,
                        "suggestion": pick["title"] if pick else None,
                        "todo_id": pick["id"] if pick else None,
                    }
                )

    # Avoidance: the important things still open and being skipped.
    avoided = [
        {"title": a["todo"]["title"], "days_open": a["days_open"], "todo_id": a["todo"]["id"]}
        for a in avoided_todos(todos, now)[:3]
    ]

    briefing = Briefing(
        date=day_start.strftime("%Y-%m-%d"),
        format=fmt_pref,
        today=today,
        conflicts=conflicts,
        slips=dict(slip_counter),
        coaching=coaching,
        spare=spare,
        avoided=avoided,
    )

    # Closing encouragement line (spec §6.2). Lazy import: encouragement.py imports
    # this module's day-band constants, so importing it at top level would cycle.
    # Inert unless the `encouragement` layer is on — returns None on an ordinary day.
    from prefrontal.encouragement import briefing_note

    return replace(briefing, encouragement=briefing_note(store, briefing, now=now))


def _time_of(commitment: dict[str, Any]) -> str:
    """Render a commitment's UTC start as ``HH:MM`` (UTC)."""
    return commitment["start_at"][11:16]


def render_briefing(briefing: Briefing) -> str:
    """Render a :class:`Briefing` as Markdown (the deterministic digest).

    Honors ``briefing.format``: ``short`` keeps it to headlines, ``long`` lists
    every commitment and slip detail.

    Args:
        briefing: The structured briefing.

    Returns:
        A Markdown string suitable for printing or delivery.
    """
    long = briefing.format == "long"
    lines = [f"# Morning briefing — {briefing.date}", ""]

    # Today.
    if not briefing.today:
        lines.append("**Today:** nothing on the calendar. 🎉")
    else:
        hard = sum(1 for c in briefing.today if c.get("hardness") == "hard")
        lines.append(
            f"**Today:** {len(briefing.today)} commitment(s)"
            + (f", {hard} hard." if hard else ".")
        )
        if long or len(briefing.today) <= 5:
            for c in briefing.today:
                mark = " (hard)" if c.get("hardness") == "hard" else ""
                cal = f" · {c['calendar']}" if c.get("calendar") else ""
                fyi = " · FYI" if c.get("kind") == "fyi" else ""
                lines.append(f"- {_time_of(c)} — {c['title']}{mark}{cal}{fyi}")
    lines.append("")

    # Conflicts.
    if briefing.conflicts:
        lines.append(f"**⚠️ Double-bookings:** {len(briefing.conflicts)}")
        for c in briefing.conflicts:
            lines.append(
                f"- '{c['a']}' overlaps '{c['b']}' by {c['overlap_minutes']:g} min"
            )
        lines.append("")

    # What slipped.
    if briefing.slips:
        total = sum(briefing.slips.values())
        detail = ", ".join(f"{n} {t}" for t, n in sorted(briefing.slips.items()))
        lines.append(f"**Slipped (last {SLIP_WINDOW_DAYS}d):** {total} — {detail}.")
        lines.append("")

    # Avoidance — the important things you keep skipping.
    if briefing.avoided:
        lines.append("**🔴 You keep putting off:**")
        for a in briefing.avoided:
            lines.append(f"- {a['title']} — open {a['days_open']:g} days")
        lines.append("")

    # Spare time + suggestions.
    suggested = [s for s in briefing.spare if s["suggestion"]]
    if suggested:
        lines.append("**Spare time:**")
        for s in suggested:
            lines.append(
                f"- {s['start'][11:16]} ({s['minutes']:g} min free) — good for: "
                f"{s['suggestion']}"
            )
        lines.append("")

    # Closing note. When the encouragement layer flagged the day (spec §6.2), its
    # line takes the place of the usual time-bias reminder — a rough/packed day
    # doesn't need a nag, and a wide-open one gets the relax-vs-accomplish choice.
    if briefing.encouragement:
        lines.append(f"_{briefing.encouragement}_")
    else:
        bias = briefing.coaching.get("time_estimation_bias")
        if bias:
            try:
                pct = round((float(bias) - 1.0) * 100)
                lines.append(
                    f"_Reminder: you tend to underestimate time by ~{pct}% — "
                    f"pad today's estimates ({bias}x)._"
                )
            except ValueError:
                pass

    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class BriefingResult:
    """Result of :func:`summarize_briefing`."""

    text: str
    source: str  # "llm" | "heuristic"
    model: str | None = None


def summarize_briefing(
    store: MemoryStore,
    *,
    client: Generator | None = None,
    now: Any | None = None,
    fallback: bool = True,
) -> BriefingResult:
    """Render the briefing and optionally rewrite it as prose via the model.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        client: A model client — the local Ollama client, or Claude when the
            ``briefing`` agent is opted into the Anthropic provider (defaults to
            a local client built from settings).
        now: Optional naive-UTC "now".
        fallback: Fall back to the deterministic digest on model failure.

    Returns:
        A :class:`BriefingResult`.

    Raises:
        prefrontal.integrations.base.ProviderError: If the model fails and
            ``fallback`` is ``False``.
    """
    from prefrontal.integrations.base import ProviderError
    from prefrontal.integrations.ollama import OllamaClient

    rendered = render_briefing(build_briefing(store, now=now))
    client = client or OllamaClient.from_settings()
    try:
        prose = client.generate(rendered, system=BRIEFING_SYSTEM_PROMPT).strip()
    except ProviderError:
        if not fallback:
            raise
        return BriefingResult(text=rendered, source="heuristic")
    if not prose:
        if not fallback:
            raise ProviderError("The model returned an empty briefing.")
        return BriefingResult(text=rendered, source="heuristic")
    return BriefingResult(text=prose, source="llm", model=client.model)
