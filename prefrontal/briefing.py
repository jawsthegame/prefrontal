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
from prefrontal.clock import parse_ts_strict as _parse_ts
from prefrontal.commitments import find_conflicts, is_attendable
from prefrontal.config import get_settings
from prefrontal.departure import departure_kwargs, plan_departure
from prefrontal.focus_balance import balance_summary_line, build_focus_balance
from prefrontal.impact import fragile_stretch, utcnow
from prefrontal.memory.patterns import task_bias_resolver
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.impulsivity import switch_rate_feedback
from prefrontal.modules.registry import is_enabled as module_enabled
from prefrontal.scheduling import (
    free_windows,
    local_datetime,
    local_day_bounds,
    local_time_utc,
    suggest_for_windows,
    window_config_for,
)
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

#: How far back the briefing pulls triage ``surface`` items (spec §12: last day).
SURFACE_WINDOW_HOURS = 24

#: How many todo options to offer per spare window (a primary + up to 2 alts).
SPARE_OPTIONS_PER_WINDOW = 3

#: Coaching-state counters for the briefing's 👍/👎 feedback. A one-tap vote in
#: the delivered digest bumps one of these; :func:`learned_briefing_guidance`
#: folds the running tally back into :data:`BRIEFING_SYSTEM_PROMPT`.
BRIEFING_HELPFUL_KEY = "briefing_helpful_count"
BRIEFING_NOT_HELPFUL_KEY = "briefing_not_helpful_count"

#: Net lead one way or the other before the feedback tally changes the voice —
#: small, so a couple of consistent votes are enough, but not a single stray tap.
_BRIEFING_FEEDBACK_MARGIN = 2


def record_briefing_feedback(store: MemoryStore, *, helpful: bool) -> dict[str, int]:
    """Record a 👍/👎 vote on the morning briefing; return the running tally.

    Bumps the matching coaching-state counter (helpful vs not), which
    :func:`learned_briefing_guidance` reads to steer the LLM briefing voice.
    Idempotency isn't attempted — a re-tap is just another honest vote, and the
    margin-based guidance shrugs off the odd extra one.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        helpful: ``True`` for 👍, ``False`` for 👎.

    Returns:
        ``{"helpful": int, "not_helpful": int}`` — the tally after this vote.
    """
    key = BRIEFING_HELPFUL_KEY if helpful else BRIEFING_NOT_HELPFUL_KEY
    store.set_state(key, str(int(store.get_float(key, 0.0)) + 1), source="explicit")
    return {
        "helpful": int(store.get_float(BRIEFING_HELPFUL_KEY, 0.0)),
        "not_helpful": int(store.get_float(BRIEFING_NOT_HELPFUL_KEY, 0.0)),
    }


def learned_briefing_guidance(store: MemoryStore) -> str:
    """Prompt addendum reflecting the running 👍/👎 feedback on past briefings.

    Folds the standing tally into the LLM briefing prompt so the voice adapts to
    how the digests have been landing: a run of 👎 steers it shorter and more
    focused; a run of 👍 tells it to hold the current shape. Empty string when
    the votes are close (no clear signal yet) — the base prompt is used unchanged.
    """
    helpful = int(store.get_float(BRIEFING_HELPFUL_KEY, 0.0))
    not_helpful = int(store.get_float(BRIEFING_NOT_HELPFUL_KEY, 0.0))
    if not_helpful - helpful >= _BRIEFING_FEEDBACK_MARGIN:
        return (
            "\n\nRecent briefings were marked *not useful*. Tighten up: lead with "
            "the single most important thing, drop anything that isn't actionable "
            "today, and keep the whole thing to two or three sentences."
        )
    if helpful - not_helpful >= _BRIEFING_FEEDBACK_MARGIN:
        return (
            "\n\nRecent briefings were marked useful — keep this shape, warmth, "
            "and length."
        )
    return ""


@dataclass(frozen=True)
class Briefing:
    """Structured morning briefing.

    Attributes:
        date: The briefing date (local calendar day, ``YYYY-MM-DD``).
        format: ``short`` or ``long`` (from coaching state).
        tz: IANA timezone the times are rendered in (the deployment's home
            zone). Stored ``start_at``/``leave_by`` values are naive UTC; the
            renderer converts them to this zone so the digest reads in local
            wall-clock time, not UTC.
        today: Today's commitments (dicts), soonest first.
        conflicts: Double-booking pairs among upcoming commitments.
        slips: Count of recent ``miss`` episodes by ``episode_type``.
        coaching: Selected coaching values surfaced in the briefing.
        spare: Free windows in the rest of the day, each with a suggested todo.
        departures: Leave-by times for today's *remaining* travel commitments —
            "when to leave" (bias-adjusted travel estimate, or the static lead),
            so the morning digest says when to head out, not only when things
            start. Empty when the Time Blindness module is off or nothing today
            has travel lead.
        fragile: At-risk links of the tightest back-to-back stretch among today's
            remaining commitments *if* each runs the learned time-bias longer than
            planned — a "before it slips" preview. Empty on a day with slack or
            when the bias shows no habitual overrun.
        encouragement: Optional closing encouragement line (spec §6.2) — set when
            the ``encouragement`` layer is on and the day warrants a note; ``None``
            leaves the briefing's usual time-bias reminder in place.
        switch_feedback: Optional switch-rate reflection (Impulsivity's
            ``switch_rate_feedback`` intervention) — set when the last day's focus
            sessions signalled any switch-impulses; ``None`` otherwise.
        balance: Optional one-line focus-balance digest — the week's out-of-home
            time by life-sphere (shop/work/home/kids/personal) from closed-loop trips;
            ``None`` when no completed trips fall in the window.
    """

    date: str
    format: str
    tz: str = "UTC"
    today: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    slips: dict[str, int] = field(default_factory=dict)
    coaching: dict[str, str] = field(default_factory=dict)
    spare: list[dict[str, Any]] = field(default_factory=list)
    avoided: list[dict[str, Any]] = field(default_factory=list)
    surfaced: list[dict[str, Any]] = field(default_factory=list)
    departures: list[dict[str, Any]] = field(default_factory=list)
    fragile: list[dict[str, Any]] = field(default_factory=list)
    encouragement: str | None = None
    switch_feedback: str | None = None
    balance: str | None = None


def build_briefing(store: MemoryStore, now: Any | None = None) -> Briefing:
    """Assemble the structured briefing from memory.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`Briefing`.
    """
    now = now or utcnow()
    tz = get_settings().timezone
    # "Today" is the user's *local* calendar day, not the UTC day — otherwise a
    # morning briefing pulled before local midnight (or an evening one after the
    # UTC rollover) shows the wrong day's commitments and slips.
    day_start, day_end = local_day_bounds(now, tz)
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
        # The available-hours band is *local* (8am–8pm your time), converted to
        # UTC — not day_start.replace(hour=8), which would be 8am UTC.
        band_start = max(now, local_time_utc(now, tz, DEFAULT_DAY_START_HOUR))
        band_end = local_time_utc(now, tz, DEFAULT_DAY_END_HOUR)
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
                options_per_window=SPARE_OPTIONS_PER_WINDOW,
            ):
                pick = s["suggestion"]
                # Alternatives = the fitting options beyond the primary, so a gap
                # offers a small menu ("or: X, Y"), not a single take-it-or-leave-it.
                alternatives = [t["title"] for t in s["options"][1:]]
                spare.append(
                    {
                        "start": s["window"].start,
                        "minutes": s["window"].minutes,
                        "suggestion": pick["title"] if pick else None,
                        "todo_id": pick["id"] if pick else None,
                        "alternatives": alternatives,
                    }
                )

    # Fragile stretch: today's *remaining* commitments previewed under the learned
    # overrun — if each runs bias× long, which back-to-backs collide? A "before it
    # slips" heads-up, distinct from the reactive cascade on a live overrun. Silent
    # on a day with slack or when the bias shows no habitual underestimation.
    try:
        bias = float(state.get("time_estimation_bias", {}).get("value", 1.0))
    except (TypeError, ValueError):
        bias = 1.0
    remaining = [c for c in today if _parse_ts(c["start_at"]) >= now]
    # Only real, own commitments sit in a "your time" chain: FYI events (where
    # someone *else* will be) and placeholder holds don't consume your time, so
    # they can't topple anything or earn a leave-by. This is the same subset every
    # cascade/departure surface uses (see commitments.is_attendable).
    attendable = [c for c in remaining if is_attendable(c)]
    fragile = [
        {
            "title": i.commitment["title"],
            "start_at": i.commitment["start_at"],
            "projected_start": i.projected_start,
            "delay_minutes": i.delay_minutes,
            "caused_by": i.caused_by,
            "hardness": i.commitment.get("hardness"),
        }
        for i in fragile_stretch(attendable, bias)
    ]

    # Leave-by for today's remaining *travel* commitments — "when to leave," not
    # just when things start. Plans the same `attendable` list with the same pure
    # planner (plan_departure + departure_kwargs) the departure nudge and widget
    # use, so the digest's leave-by matches what it's nudged for. Gated on Time
    # Blindness (which owns departure timing); attend-mode meetings (you're already
    # there) and zero-lead items are omitted.
    departures: list[dict[str, Any]] = []
    if module_enabled("time_blindness"):
        loc = store.get_location()
        dep_kwargs = departure_kwargs(store)
        # `attendable` (above) is the same subset the departure nudge itself uses
        # (departure.plan_upcoming_departures), so the digest's leave-by matches
        # what it's nudged for — no FYI event or placeholder hold ever gets one.
        for c in attendable:
            p = plan_departure(
                c,
                current_lat=loc["lat"] if loc else None,
                current_lon=loc["lon"] if loc else None,
                now=now,
                **dep_kwargs,
            )
            if p.mode != "travel" or _parse_ts(p.leave_by) >= _parse_ts(c["start_at"]):
                continue  # attend-from-here, or no real lead — nothing to say
            departures.append(
                {
                    "title": c["title"],
                    "commitment_id": c.get("id"),
                    "start_at": c["start_at"],
                    "leave_by": p.leave_by,
                    "travel_minutes": p.travel_minutes,
                    "basis": p.basis,
                }
            )

    # Avoidance: the important things still open and being skipped.
    avoided = [
        {"title": a["todo"]["title"], "days_open": a["days_open"], "todo_id": a["todo"]["id"]}
        for a in avoided_todos(todos, now)[:3]
    ]

    # Triage "surface" items — worth seeing once, no core-table write — from the
    # last day (older ones age out of view without deletion, spec §12).
    surface_since = (now - timedelta(hours=SURFACE_WINDOW_HOURS)).strftime(fmt)
    surfaced = [
        {"title": s["title"], "reason": s["reason"], "source": s["source"]}
        for s in store.surfaced_triage(surface_since)
    ]

    # Switch-rate reflection (Impulsivity, `switch_rate_feedback`): the last day's
    # closed focus sessions vs the learned baseline. Silent when nothing switched.
    switch_since = (now - timedelta(days=1)).strftime(fmt)
    recent_focus = [
        s
        for s in store.recent_focus_sessions()
        if s.get("ended_at") and s["ended_at"] >= switch_since
    ]
    switch_feedback = switch_rate_feedback(
        recent_focus, baseline=_switch_baseline(store)
    )

    # Focus balance: a one-line "where the week's out-of-home time went" digest by
    # life-sphere (shop/work/home/kids/personal), from closed-loop trips. None when no
    # completed trips land in the window, so an untracked week adds no noise.
    balance = balance_summary_line(build_focus_balance(store, now=now))

    briefing = Briefing(
        date=day_start.strftime("%Y-%m-%d"),
        format=fmt_pref,
        tz=tz,
        today=today,
        conflicts=conflicts,
        slips=dict(slip_counter),
        coaching=coaching,
        spare=spare,
        avoided=avoided,
        surfaced=surfaced,
        departures=departures,
        fragile=fragile,
        switch_feedback=switch_feedback,
        balance=balance,
    )

    # Closing encouragement line (spec §6.2). Lazy import: encouragement.py imports
    # this module's day-band constants, so importing it at top level would cycle.
    # Inert unless the `encouragement` layer is on — returns None on an ordinary day.
    from prefrontal.encouragement import briefing_note

    return replace(briefing, encouragement=briefing_note(store, briefing, now=now))


#: Confidence floor before the learned switch baseline is worth comparing against.
_SWITCH_BASELINE_FLOOR = 0.3


def _switch_baseline(store: MemoryStore) -> float | None:
    """The learned mean *honored* switch-impulses per focus session, or ``None``.

    Reads the ``context_switch`` pattern (``variance`` = mean honored/session) and
    returns it only once confidence clears a small floor, so an early, noisy
    estimate isn't presented as a norm.
    """
    for p in store.get_patterns("context_switch"):
        variance = p.get("variance")
        if variance is not None and (p.get("confidence") or 0.0) >= _SWITCH_BASELINE_FLOOR:
            return float(variance)
    return None


def _local_hm(ts: str, tz: str) -> str:
    """Render a stored naive-UTC timestamp as local ``HH:MM`` in ``tz``.

    Stored times are naive UTC; slicing ``[11:16]`` off the string would print
    the UTC wall clock (e.g. 4h ahead for an Eastern user). Convert first so the
    digest reads in the reader's own time.
    """
    return local_datetime(_parse_ts(ts), tz).strftime("%H:%M")


def _time_of(commitment: dict[str, Any], tz: str) -> str:
    """Render a commitment's start as local ``HH:MM`` in ``tz``."""
    return _local_hm(commitment["start_at"], tz)


def render_briefing(
    briefing: Briefing, *, feedback_urls: tuple[str, str] | None = None
) -> str:
    """Render a :class:`Briefing` as Markdown (the deterministic digest).

    The digest is organized into three scannable zones under ``##`` headers, so
    the eye can land on the part it wants instead of reading a flat wall of
    equal-weight lines:

    - **📅 Today** — the schedule and what to act on now (leave-by, double-bookings,
      the tight-stretch preview).
    - **🎯 On your radar** — things you *could* pick up (what keeps sliding, triage's
      worth-a-look items, spare-time suggestions).
    - **🔎 Looking back** — the gentler weekly view (what slipped, focus switches,
      focus balance), sitting by the closing note.

    A zone's header is only emitted when it has something to show, so a quiet day
    stays short. Within a zone each item is one calm line (or a small bullet list),
    separated by blank lines — no stacked bold headers competing for attention.

    Honors ``briefing.format``: ``short`` keeps it to headlines, ``long`` lists
    every commitment.

    Args:
        briefing: The structured briefing.
        feedback_urls: Optional ``(helped_url, not_helped_url)`` signed one-tap
            links. When both are non-empty a small "Did this help?" footer is
            appended; the taps feed :func:`learned_briefing_guidance` back into the
            LLM briefing voice. Omit (or pass empty strings) to render no footer —
            e.g. for a plain CLI print with no public origin to link to.

    Returns:
        A Markdown string suitable for printing or delivery.
    """
    long = briefing.format == "long"
    lines: list[str] = [f"# Morning briefing — {briefing.date}"]

    def block(*item_lines: str) -> None:
        """Append a blank-line-separated block so blocks never run together.

        Markdown collapses adjacent non-list lines into one paragraph, so every
        distinct one-liner needs its own block; the leading blank is what gives
        the digest its breathing room.
        """
        lines.append("")
        lines.extend(item_lines)

    # ── 📅 Today — the schedule and what to act on now ─────────────────────────
    if not briefing.today:
        block("## 📅 Today", "You've got nothing on the calendar. 🎉")
    else:
        n = len(briefing.today)
        hard = sum(1 for c in briefing.today if c.get("hardness") == "hard")
        noun = "commitment" if n == 1 else "commitments"
        head = f"## 📅 Today · {n} {noun}" + (f" ({hard} hard)" if hard else "")
        rows: list[str] = []
        if long or n <= 5:
            for c in briefing.today:
                mark = " (hard)" if c.get("hardness") == "hard" else ""
                cal = f" · {c['calendar']}" if c.get("calendar") else ""
                fyi = " · FYI" if c.get("kind") == "fyi" else ""
                rows.append(f"- {_time_of(c, briefing.tz)} — {c['title']}{mark}{cal}{fyi}")
        block(head, *rows)

    # Leave by — when to head out for today's remaining travel commitments, so the
    # digest surfaces the actionable time (departure), not only the start time.
    for d in briefing.departures:
        travel = (
            f" (~{round(d['travel_minutes'])} min travel)"
            if d.get("travel_minutes") is not None
            else ""
        )
        block(f"🚶 Leave by {_local_hm(d['leave_by'], briefing.tz)} for {d['title']}{travel}")

    # Conflicts.
    for c in briefing.conflicts:
        block(
            f"⚠️ Double-booking: '{c['a']}' overlaps '{c['b']}' by "
            f"{c['overlap_minutes']:g} min"
        )

    # Fragile stretch — a back-to-back run that holds on paper but topples if you
    # run your usual bit long. A preview, not an alarm: framed as "if things run
    # long," so it informs without nagging.
    if briefing.fragile:
        shown = briefing.fragile[:3]
        chain = " → ".join(
            f"{f['title']} (~{round(f['delay_minutes'])} min late)" for f in shown
        )
        more = "" if len(briefing.fragile) <= 3 else f" +{len(briefing.fragile) - 3} more"
        trigger = briefing.fragile[0].get("caused_by") or "the first"
        block(
            f"⏳ Tight stretch: if today runs long, {chain}{more} — "
            f"it starts with '{trigger}' overrunning."
        )

    # ── 🎯 On your radar — things you could pick up ────────────────────────────
    suggested = [s for s in briefing.spare if s["suggestion"]]
    if briefing.avoided or briefing.surfaced or suggested:
        block("## 🎯 On your radar")

        # Avoidance — the important things that keep sliding. Named plainly, but
        # without a scolding "you keep …": a nudge to pick one up, not a telling-off.
        if briefing.avoided:
            block(
                "🐢 Keeps sliding",
                *[f"- {a['title']} — open {a['days_open']:g} days" for a in briefing.avoided],
            )

        # Worth a look — triage surfaced these (seen once, no action taken for you).
        if briefing.surfaced:
            block(
                "📥 Worth a look",
                *[
                    f"- {s['title']}" + (f" ({s['source']})" if s.get("source") else "")
                    for s in briefing.surfaced[:5]
                ],
            )

        # Spare time + suggestions.
        if suggested:
            spare_lines: list[str] = []
            for s in suggested:
                alts = s.get("alternatives") or []
                spare_lines.append(
                    f"- {_local_hm(s['start'], briefing.tz)} "
                    f"({s['minutes']:g} min free) — {s['suggestion']}"
                )
                if alts:
                    spare_lines.append(f"  _or: {', '.join(alts)}_")
            block("✨ Spare time", *spare_lines)

    # ── 🔎 Looking back — the gentler weekly view ──────────────────────────────
    if briefing.slips or briefing.switch_feedback or briefing.balance:
        block("## 🔎 Looking back")

        # What slipped.
        if briefing.slips:
            total = sum(briefing.slips.values())
            detail = ", ".join(f"{n} {t}" for t, n in sorted(briefing.slips.items()))
            block(f"📉 Slipped (last {SLIP_WINDOW_DAYS}d): {total} — {detail}.")

        # Switch-rate reflection (Impulsivity) — deferrals framed as the win.
        if briefing.switch_feedback:
            block(f"🔁 Focus switches (last 24h): {briefing.switch_feedback}.")

        # Focus balance — where the week's out-of-home time went, by life-sphere.
        if briefing.balance:
            block(f"⚖️ Focus balance: {briefing.balance}")

    # Closing note. When the encouragement layer flagged the day (spec §6.2), its
    # line takes the place of the usual time-bias reminder — a rough/packed day
    # doesn't need a nag, and a wide-open one gets the relax-vs-accomplish choice.
    if briefing.encouragement:
        block(f"_{briefing.encouragement}_")
    else:
        bias = briefing.coaching.get("time_estimation_bias")
        if bias:
            try:
                pct = round((float(bias) - 1.0) * 100)
            except ValueError:
                pct = 0
            # Only nudge when there's a real learned overrun — "~0%" is noise.
            if pct > 0:
                block(
                    f"_Reminder: you tend to underestimate time by ~{pct}% — "
                    f"give today's plans a little extra room._"
                )

    body = "\n".join(lines).rstrip()

    # Feedback footer — a small, optional 👍/👎 so a tap can teach the briefing
    # voice (see learned_briefing_guidance). Appended after the rstrip so it's the
    # last thing in the digest, and only when both signed links are available.
    if feedback_urls and all(feedback_urls):
        helped, not_helped = feedback_urls
        body += f"\n\n—\n_Did this help? [👍 yes]({helped}) · [👎 no]({not_helped})_"

    return body + "\n"


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
    # Fold the running 👍/👎 feedback into the voice — a run of "not useful" taps
    # tightens the prose, a run of "useful" taps holds the current shape.
    system = BRIEFING_SYSTEM_PROMPT + learned_briefing_guidance(store)
    try:
        prose = client.generate(rendered, system=system).strip()
    except ProviderError:
        if not fallback:
            raise
        return BriefingResult(text=rendered, source="heuristic")
    if not prose:
        if not fallback:
            raise ProviderError("The model returned an empty briefing.")
        return BriefingResult(text=rendered, source="heuristic")
    return BriefingResult(text=prose, source="llm", model=client.model)
