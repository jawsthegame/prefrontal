"""Visual day-shape — the day laid out as a sequence of concrete blocks.

The briefing tells you *what's* on today; panic tells you what's *on fire*; "one
next thing" tells you the *single* next move. This is the missing spatial view:
the whole day rendered as a **timeline** — fixed commitments as anchored blocks,
open todos fitted into the gaps between them, and the free time in between — so
the day's *shape* is glanceable and concrete rather than a list you have to hold
in your head. It's the Structured / Tiimo pattern, built from what Prefrontal
already knows.

The point is externalization: ADHD time-blindness makes an abstract schedule
("dentist at 2, then the report") hard to *feel*. A proportional visual — this
block is twice as tall because it's twice as long, this gap is where the report
could go — turns the day into something you can see at a glance instead of
reconstruct.

Like the briefing, panic, and next-thing, this is a **deterministic, model-free
core** (:func:`build_day_shape` / :func:`render_day_shape`): fully testable, no
LLM in the path, safe to poll on a widget's timeline cadence. It reuses the exact
fitting machinery the briefing already trusts — :func:`~prefrontal.scheduling.free_windows`
and :func:`~prefrontal.scheduling.suggest_for_windows` — so the day-shape and the
morning digest never disagree about where a todo fits.

**Designed for how ADHD readers actually read a chart.** The CHI-2024 work on
data visualization for ADHD found that color and contrast are read *differently* —
a legend that leans on hue alone, or low-contrast fills to convey category, don't
land. So this core never encodes meaning in color alone: every segment carries a
redundant, non-color signal — a ``kind`` label, a ``glyph``, and a ``pattern``
(``solid`` / ``dashed`` / ``dotted``) — so the client can (and the CLI does) draw
the day legibly in monochrome. Commitments are firm blocks; fitted todos are
clearly marked *suggestions* (you can move them); free time is quiet, not empty.

The surface is deliberately **read-mostly and forgiving** (the abandonment-trap
guard, roadmap M2): the past is dimmed, not scored; there is no overdue-red, no
streak, no guilt. Re-opening it after a lapse shows *today*, not a backlog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import (
    TS_FMT,
    local_datetime,
    local_day_bounds,
    local_time_utc,
)
from prefrontal.clock import parse_ts_strict as _parse
from prefrontal.commitments import is_attendable
from prefrontal.scheduling import (
    DEFAULT_EVENT_MINUTES,
    DEFAULT_MIN_WINDOW_MINUTES,
    FreeWindow,
    free_windows,
    suggest_for_windows,
    window_config_for,
)
from prefrontal.todos import avoided_todos

#: The floor a fitted-todo block is padded up to, so a quick 5-minute task is
#: still a tappable target rather than a sliver — *unless the gap itself is
#: shorter*, in which case the block is capped at the gap (it never overflows it).
_MIN_TODO_BLOCK_MINUTES = 15.0
#: How many todo options to offer per gap (a primary + up to 2 alternatives),
#: matching the briefing's spare-time menu so the two surfaces agree.
_OPTIONS_PER_WINDOW = 3


@dataclass(frozen=True)
class Segment:
    """One block on the day's timeline — a commitment, a fitted todo, or free time.

    Segments tile the visual day in chronological order. Every segment carries a
    **non-color** signal for its kind (``glyph`` + ``pattern`` + the ``kind`` word
    itself) so the timeline reads legibly without relying on hue — the CHI-2024
    finding that ADHD readers don't reliably decode color-only encodings.

    Attributes:
        kind: ``commitment`` | ``todo`` | ``free``.
        start_at / end_at: The block's UTC span (``YYYY-MM-DD HH:MM:SS``).
        start_local / end_local: The same span as local ``HH:MM`` wall-clock, for
            display without the client re-deriving the zone.
        minutes: Block length in minutes (drives its proportional height).
        title: What the block is — a commitment's title, a todo's title, or
            ``"Free"`` for a gap.
        state: ``past`` (ended before now) | ``now`` (in progress) | ``upcoming``.
            The past is dimmed, never scored — the forgiving, no-guilt surface.
        glyph: A short redundant marker (emoji) for the kind/flavour, so color is
            never the only cue.
        pattern: The border/fill treatment a client should use — ``solid`` (a firm
            commitment), ``dashed`` (a movable suggested todo), ``dotted`` (quiet
            free time). A second non-color channel alongside ``glyph``.
        detail: A short subtitle (a commitment's location/calendar, a todo's
            estimate + why it surfaced, a gap's "open" note). ``None`` when empty.
        hard: ``True`` for a firm ("hard") commitment — surfaced as a word, not a
            colour.
        fyi: ``True`` for an FYI block (someone *else*'s event) — shown so the day
            reads true, marked so it's clear it isn't yours to attend.
        conflict: ``True`` when this commitment overlaps the previous one (a
            double-booking) — flagged in words, not by colour alone.
        reason: For a fitted todo, *why this one* — ``avoided`` (you keep skipping
            it) or ``fits`` (it simply fits the gap); ``None`` otherwise.
        estimate_minutes: A fitted todo's own estimate (before bias), when known.
        alternatives: For a fitted todo, other todos that also fit this gap
            (titles), so the block can offer a small "or:" menu rather than one
            take-it-or-leave-it pick.
        commitment_id / todo_id: The source row id, for deep-linking / one-tap.
    """

    kind: str
    start_at: str
    end_at: str
    start_local: str
    end_local: str
    minutes: float
    title: str
    state: str
    glyph: str
    pattern: str
    detail: str | None = None
    hard: bool = False
    fyi: bool = False
    conflict: bool = False
    reason: str | None = None
    estimate_minutes: int | None = None
    alternatives: list[str] = field(default_factory=list)
    commitment_id: int | None = None
    todo_id: int | None = None


@dataclass(frozen=True)
class DayShape:
    """The day's structure as a chronological sequence of :class:`Segment`s.

    Attributes:
        date: The local calendar day (``YYYY-MM-DD``).
        tz: IANA timezone the local times are rendered in.
        band_start / band_end: The visual day's UTC bounds — the waking band,
            widened to contain any commitment that starts early or ends late, so
            nothing on the calendar is clipped off the edge of the timeline.
        band_start_local / band_end_local: The same bounds as local ``HH:MM``.
        now: "Now" as naive UTC (the instant the shape was built).
        now_local: "Now" as local ``HH:MM``.
        now_fraction: Where *now* sits along the band, ``0.0``–``1.0``, for a
            "you are here" marker — or ``None`` when now is outside the band (the
            day hasn't started, or is already done).
        segments: The blocks, chronological.
        committed_minutes: Total minutes held by your own (attendable) commitments.
        free_minutes: Total minutes of open time still ahead (from now on).
        fitted_count: How many open todos got a slot in the day.
    """

    date: str
    tz: str
    band_start: str
    band_end: str
    band_start_local: str
    band_end_local: str
    now: str
    now_local: str
    now_fraction: float | None
    segments: list[Segment] = field(default_factory=list)
    committed_minutes: float = 0.0
    free_minutes: float = 0.0
    fitted_count: int = 0


def _hhmm_parts(hhmm: str) -> tuple[int, int]:
    """Split a ``"HH:MM"`` string into ``(hour, minute)`` (assumes well-formed)."""
    h, m = hhmm.split(":")
    return int(h), int(m)


def _local_hm(ts_or_dt: str | datetime, tz: str) -> str:
    """Render a naive-UTC timestamp (or datetime) as local ``HH:MM`` in ``tz``."""
    dt = _parse(ts_or_dt) if isinstance(ts_or_dt, str) else ts_or_dt
    return local_datetime(dt, tz).strftime("%H:%M")


def _state_of(start: datetime, end: datetime, now: datetime) -> str:
    """Classify a block against *now* — ``past`` / ``now`` / ``upcoming``."""
    if end <= now:
        return "past"
    if start <= now < end:
        return "now"
    return "upcoming"


def _commitment_end(c: dict[str, Any]) -> datetime:
    """A commitment's effective end — ``end_at`` if set, else a default duration.

    Mirrors :func:`prefrontal.scheduling.free_windows`, so a point event with no
    ``end_at`` occupies the same assumed span on the timeline as it does when
    gaps are carved around it.
    """
    start = _parse(c["start_at"])
    if c.get("end_at"):
        end = _parse(c["end_at"])
        if end > start:
            return end
    return start + timedelta(minutes=DEFAULT_EVENT_MINUTES)


def build_day_shape(
    store: Any, now: datetime | None = None, *, settings: Any | None = None
) -> DayShape:
    """Assemble the visual day-shape from memory (deterministic, model-free).

    Lays today's commitments out as fixed blocks and fits the open todos into the
    gaps between them — reusing :func:`~prefrontal.scheduling.free_windows` and
    :func:`~prefrontal.scheduling.suggest_for_windows` (the same fitting the
    morning briefing uses, so the two never disagree). Only *forward-looking* gaps
    (ending after now) get a suggested todo — the past is drawn but never
    back-fills a "you could have…" block. Every segment is tagged with a
    non-color kind signal (``glyph`` + ``pattern``) for legibility without hue.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore` (user-scoped).
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.clock.utcnow`).
        settings: Optional resolved settings (timezone + window config). Defaults
            to :func:`prefrontal.config.get_settings`.

    Returns:
        A :class:`DayShape`.
    """
    from prefrontal.clock import utcnow
    from prefrontal.config import get_settings

    now = now or utcnow()
    settings = settings or get_settings()
    tz = settings.timezone

    day_start, day_end = local_day_bounds(now, tz)
    today = store.commitments_between(day_start.strftime(TS_FMT), day_end.strftime(TS_FMT))

    # The visual band is the local waking window (the off-zone's complement, or
    # today's configured "available hours"), so the timeline shows a real day, not
    # a dead 24h strip. band_for_weekday returns None on a day the user marked
    # unavailable — fall back to the flat awake band so there's always something to
    # draw.
    window_config = window_config_for(settings, store)
    weekday = local_datetime(now, tz).weekday()
    band = window_config.band_for_weekday(weekday) or window_config.awake_band()
    sh, sm = _hhmm_parts(band[0])
    eh, em = _hhmm_parts(band[1])
    band_start = local_time_utc(now, tz, sh, sm)
    band_end = local_time_utc(now, tz, eh, em)

    # Widen the band to contain any commitment that pokes out of the waking window
    # (an early flight, a late show) so nothing on the calendar is clipped off the
    # edge — but clamp to the local day so a stray multi-day block can't stretch the
    # strip into a wall.
    for c in today:
        band_start = min(band_start, _parse(c["start_at"]))
        band_end = max(band_end, _commitment_end(c))
    band_start = max(band_start, day_start)
    band_end = min(max(band_end, band_start + timedelta(minutes=1)), day_end)

    segments: list[Segment] = []

    # ── Commitment blocks — the fixed anchors ────────────────────────────────
    # Sorted by start (commitments_between already is), each becomes a solid block.
    # A block whose start precedes the previous block's end is a double-booking:
    # flagged in words, not colour.
    prev_end: datetime | None = None
    committed_minutes = 0.0
    for c in today:
        cs = _parse(c["start_at"])
        ce = _commitment_end(c)
        # Clip to the band so the proportional height is honest (an all-day block
        # spilling past the band shouldn't dwarf everything).
        vs, ve = max(cs, band_start), min(ce, band_end)
        if ve <= vs:
            continue
        is_fyi = c.get("kind") == "fyi"
        is_hard = c.get("hardness") == "hard"
        conflict = prev_end is not None and cs < prev_end
        if not is_fyi:
            committed_minutes += (ve - vs).total_seconds() / 60.0
        detail_bits: list[str] = []
        if c.get("location"):
            detail_bits.append(str(c["location"]))
        if c.get("calendar"):
            detail_bits.append(str(c["calendar"]))
        segments.append(
            Segment(
                kind="commitment",
                start_at=vs.strftime(TS_FMT),
                end_at=ve.strftime(TS_FMT),
                start_local=_local_hm(vs, tz),
                end_local=_local_hm(ve, tz),
                minutes=round((ve - vs).total_seconds() / 60.0, 1),
                title=c.get("title") or "Commitment",
                state=_state_of(cs, ce, now),
                glyph="📌",
                pattern="solid",
                detail=" · ".join(detail_bits) or None,
                hard=is_hard,
                fyi=is_fyi,
                conflict=conflict,
                commitment_id=c.get("id"),
            )
        )
        prev_end = ce if prev_end is None else max(prev_end, ce)

    # ── Free windows, with the open todos fitted into the forward ones ────────
    # Only real, own commitments carve up free time. FYI events (someone else's)
    # and placeholder/hold blocks are still *drawn* as segments above, but they
    # must not consume the gaps todos get fitted into — otherwise an FYI hour is
    # neither committed nor available and silently vanishes. Mirrors
    # availability.constraint_commitments / scheduling.suggest_now.
    raw_windows = free_windows(
        [c for c in today if is_attendable(c)], band_start, band_end
    )

    # Only forward-looking gaps get a suggestion, clipped so the fit is measured
    # against the time actually left in the gap (not a slot that's half gone).
    fit_windows: list[FreeWindow] = []
    for w in raw_windows:
        we = _parse(w.end)
        if we <= now:
            continue
        cs = max(_parse(w.start), now)
        mins = (we - cs).total_seconds() / 60.0
        if mins < DEFAULT_MIN_WINDOW_MINUTES:
            continue
        fit_windows.append(FreeWindow(cs.strftime(TS_FMT), w.end, round(mins, 1)))

    todos = store.open_todos(exclude_delegated=True, with_project_rank=True)
    bias = _flat_bias(store)
    suggestions: dict[str, dict[str, Any]] = {}
    if todos and fit_windows:
        from prefrontal.memory.patterns import task_bias_resolver

        for s in suggest_for_windows(
            fit_windows,
            todos,
            bias,
            config=window_config,
            tz=tz,
            resolver_for_hour=lambda hour: task_bias_resolver(store, local_hour=hour),
            options_per_window=_OPTIONS_PER_WINDOW,
        ):
            suggestions[s["window"].start] = s

    avoided_ids = {a["todo"]["id"] for a in avoided_todos(todos, now)}
    free_minutes = 0.0
    fitted_count = 0

    for w in raw_windows:
        ws, we = _parse(w.start), _parse(w.end)
        # The part already behind us is quiet free time — drawn (so the morning
        # still has shape) but never a back-dated suggestion.
        if ws < now:
            past_end = min(we, now)
            if (past_end - ws).total_seconds() / 60.0 >= 1.0:
                segments.append(_free_segment(ws, past_end, now, tz))
            fwd_start = now
        else:
            fwd_start = ws
        if we <= fwd_start:
            continue

        forward_min = (we - fwd_start).total_seconds() / 60.0
        free_minutes += forward_min
        s = suggestions.get(fwd_start.strftime(TS_FMT))
        pick = s["suggestion"] if s else None
        if pick is None:
            segments.append(_free_segment(fwd_start, we, now, tz))
            continue

        # Size the todo block to its bias-adjusted estimate, capped at the gap and
        # floored so a quick task is still a real target. The remainder stays free.
        est = pick.get("estimate_minutes")
        effective = (est or 0) * bias
        block_min = max(_MIN_TODO_BLOCK_MINUTES, effective)
        block_min = min(block_min, forward_min)
        block_end = fwd_start + timedelta(minutes=block_min)
        reason = "avoided" if pick["id"] in avoided_ids else "fits"
        alts = [t["title"] for t in s["options"][1:]] if s else []
        est_int = int(est) if est is not None else None
        detail = _todo_detail(est_int, reason)
        segments.append(
            Segment(
                kind="todo",
                start_at=fwd_start.strftime(TS_FMT),
                end_at=block_end.strftime(TS_FMT),
                start_local=_local_hm(fwd_start, tz),
                end_local=_local_hm(block_end, tz),
                minutes=round(block_min, 1),
                title=pick.get("title") or "Todo",
                state=_state_of(fwd_start, block_end, now),
                glyph="✨" if reason == "fits" else "🐢",
                pattern="dashed",
                detail=detail,
                reason=reason,
                estimate_minutes=est_int,
                alternatives=alts,
                todo_id=pick.get("id"),
            )
        )
        fitted_count += 1
        # Draw whatever's left of the gap after the block, down to a 1-minute
        # epsilon — the same floor the past-gap segment uses above — so the
        # segments genuinely tile the day and a real sliver of free time isn't
        # silently dropped.
        if (we - block_end).total_seconds() / 60.0 >= 1.0:
            segments.append(_free_segment(block_end, we, now, tz))

    segments.sort(key=lambda s: (s.start_at, s.end_at))

    # band_end is exclusive: at exactly the end of the band the day is done, and a
    # client that splices the marker before the first *upcoming* block has nowhere
    # to place it — so report no marker (None) rather than a fraction implying one.
    total = (band_end - band_start).total_seconds() / 60.0
    now_fraction: float | None = None
    if band_start <= now < band_end and total > 0:
        now_fraction = round((now - band_start).total_seconds() / 60.0 / total, 4)

    return DayShape(
        date=day_start.strftime("%Y-%m-%d"),
        tz=tz,
        band_start=band_start.strftime(TS_FMT),
        band_end=band_end.strftime(TS_FMT),
        band_start_local=_local_hm(band_start, tz),
        band_end_local=_local_hm(band_end, tz),
        now=now.strftime(TS_FMT),
        now_local=_local_hm(now, tz),
        now_fraction=now_fraction,
        segments=segments,
        committed_minutes=round(committed_minutes, 1),
        free_minutes=round(free_minutes, 1),
        fitted_count=fitted_count,
    )


def _flat_bias(store: Any) -> float:
    """The learned flat time-estimation bias (``>= 1``), or 1.0 when unset/garbled."""
    try:
        return float(store.all_state().get("time_estimation_bias", {}).get("value", 1.0))
    except (TypeError, ValueError, AttributeError):
        return 1.0


def _todo_detail(estimate: int | None, reason: str) -> str:
    """The subtitle for a fitted-todo block — estimate plus *why it surfaced*."""
    parts: list[str] = []
    if estimate is not None:
        parts.append(f"~{estimate} min")
    parts.append("you keep skipping this" if reason == "avoided" else "fits here")
    return " · ".join(parts)


def _free_segment(start: datetime, end: datetime, now: datetime, tz: str) -> Segment:
    """A quiet free-time block for the gap ``[start, end)`` (dotted, no suggestion)."""
    mins = round((end - start).total_seconds() / 60.0, 1)
    return Segment(
        kind="free",
        start_at=start.strftime(TS_FMT),
        end_at=end.strftime(TS_FMT),
        start_local=_local_hm(start, tz),
        end_local=_local_hm(end, tz),
        minutes=mins,
        title="Free",
        state=_state_of(start, end, now),
        glyph="○",
        pattern="dotted",
        detail="open",
    )


# --- Serialization + render --------------------------------------------------


def _segment_payload(s: Segment) -> dict[str, Any]:
    """A :class:`Segment` as a JSON-friendly dict (the ``GET /day`` wire shape)."""
    return {
        "kind": s.kind,
        "start_at": s.start_at,
        "end_at": s.end_at,
        "start_local": s.start_local,
        "end_local": s.end_local,
        "minutes": s.minutes,
        "title": s.title,
        "state": s.state,
        "glyph": s.glyph,
        "pattern": s.pattern,
        "detail": s.detail,
        "hard": s.hard,
        "fyi": s.fyi,
        "conflict": s.conflict,
        "reason": s.reason,
        "estimate_minutes": s.estimate_minutes,
        "alternatives": s.alternatives,
        "commitment_id": s.commitment_id,
        "todo_id": s.todo_id,
    }


def day_shape_payload(shape: DayShape) -> dict[str, Any]:
    """A :class:`DayShape` as a JSON-friendly dict for ``GET /day``."""
    return {
        "date": shape.date,
        "tz": shape.tz,
        "band_start": shape.band_start,
        "band_end": shape.band_end,
        "band_start_local": shape.band_start_local,
        "band_end_local": shape.band_end_local,
        "now": shape.now,
        "now_local": shape.now_local,
        "now_fraction": shape.now_fraction,
        "committed_minutes": shape.committed_minutes,
        "free_minutes": shape.free_minutes,
        "fitted_count": shape.fitted_count,
        "segments": [_segment_payload(s) for s in shape.segments],
    }


def _fmt_dur(minutes: float) -> str:
    """Compact human duration — ``45 min`` / ``1h`` / ``1h 30m``."""
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h}h {r}m" if r else f"{h}h"


def render_day_shape(shape: DayShape) -> str:
    """Render a :class:`DayShape` as a monochrome vertical timeline (for the CLI).

    Deliberately colour-free — the CHI-2024 legibility point taken to its literal
    conclusion: the day still reads as a shape in a plain terminal, using the glyph,
    the kind, and indentation as the channels. A ``▸`` marks the block in progress;
    the past is shown plainly, not scored.
    """
    lines = [f"# Today — {shape.date}", ""]
    if not shape.segments:
        lines.append("_Nothing scheduled and no free time in your waking hours today._")
        return "\n".join(lines).rstrip() + "\n"

    summary = (
        f"{shape.band_start_local}–{shape.band_end_local} · "
        f"{_fmt_dur(shape.committed_minutes)} committed · "
        f"{_fmt_dur(shape.free_minutes)} free ahead"
    )
    if shape.fitted_count:
        noun = "todo" if shape.fitted_count == 1 else "todos"
        summary += f" · {shape.fitted_count} {noun} fitted in"
    lines.append(f"_{summary}_")
    lines.append("")

    for s in shape.segments:
        marker = "▸" if s.state == "now" else " "
        span = f"{s.start_local}–{s.end_local}"
        tags: list[str] = []
        if s.hard:
            tags.append("hard")
        if s.fyi:
            tags.append("FYI")
        if s.conflict:
            tags.append("⚠ double-booked")
        title = s.title
        if s.kind == "todo":
            title = f"{title} — suggested"
        suffix = f"  ({', '.join(tags)})" if tags else ""
        dur = _fmt_dur(s.minutes)
        line = f"{marker} {span}  {s.glyph} {title}{suffix}  · {dur}"
        if s.state == "past":
            line += "  ·  done/passed"
        lines.append(line)
        if s.kind == "todo" and s.alternatives:
            lines.append(f"      or: {', '.join(s.alternatives)}")

    return "\n".join(lines).rstrip() + "\n"
