"""Fitting open todos into free time.

A todo list is only half the battle; the hard part for executive function is
*actually slotting things in*. This module finds the gaps between your
commitments and works out which open todos genuinely fit — applying the learned
time bias so a "10-minute" call is treated as the ~14 minutes it really takes.

Pure functions; the store provides the todos and commitments, and the endpoints /
briefing wire them together.

Two modes:

- **On-demand** — :func:`fit_todos`: "I have 20 minutes right now — what can I
  knock out?" Ranks the open todos that fit.
- **Proactive** — :func:`free_windows` + :func:`suggest_for_windows`: find the
  day's open gaps and propose one todo per gap (used by the morning briefing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

#: Assumed duration for a commitment with no ``end_at`` when carving windows.
DEFAULT_EVENT_MINUTES = 30.0
#: Ignore gaps shorter than this — not worth surfacing.
DEFAULT_MIN_WINDOW_MINUTES = 10.0
#: Default working-hours band for "what fits right now" (local time). Outside it
#: the widget offers nothing; inside, free time is bounded by the day's end.
DEFAULT_DAY_START = "08:30"
DEFAULT_DAY_END = "17:30"
#: Default cap on "free time right now" so an open evening doesn't offer a 3h task.
DEFAULT_FIT_CAP_MINUTES = 90.0


def _parse(ts: str) -> datetime:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` UTC timestamp."""
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class FreeWindow:
    """An open gap between commitments.

    Attributes:
        start: UTC start (``YYYY-MM-DD HH:MM:SS``).
        end: UTC end.
        minutes: Length in minutes.
    """

    start: str
    end: str
    minutes: float


def free_windows(
    commitments: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    min_minutes: float = DEFAULT_MIN_WINDOW_MINUTES,
    default_event_minutes: float = DEFAULT_EVENT_MINUTES,
) -> list[FreeWindow]:
    """Compute open windows in ``[start, end)`` not covered by commitments.

    Args:
        commitments: Commitment dicts (with ``start_at`` and optional ``end_at``).
        start: Start of the available band (naive UTC) — e.g. now, or the day's
            available-hours start.
        end: End of the available band.
        min_minutes: Drop gaps shorter than this.
        default_event_minutes: Assumed length for commitments without ``end_at``.

    Returns:
        The free windows, in chronological order.
    """
    busy: list[tuple[datetime, datetime]] = []
    for c in commitments:
        s = _parse(c["start_at"])
        e = _parse(c["end_at"]) if c.get("end_at") else s + timedelta(minutes=default_event_minutes)
        if e <= start or s >= end:
            continue
        busy.append((max(s, start), min(e, end)))
    busy.sort()

    windows: list[FreeWindow] = []
    cursor = start
    fmt = "%Y-%m-%d %H:%M:%S"
    for s, e in busy:
        if s > cursor:
            gap = (s - cursor).total_seconds() / 60.0
            if gap >= min_minutes:
                windows.append(FreeWindow(cursor.strftime(fmt), s.strftime(fmt), round(gap, 1)))
        cursor = max(cursor, e)
    if end > cursor:
        gap = (end - cursor).total_seconds() / 60.0
        if gap >= min_minutes:
            windows.append(FreeWindow(cursor.strftime(fmt), end.strftime(fmt), round(gap, 1)))
    return windows


def work_window_now(
    now: datetime,
    tz: str,
    *,
    cap_minutes: float = DEFAULT_FIT_CAP_MINUTES,
    day_start: str = DEFAULT_DAY_START,
    day_end: str = DEFAULT_DAY_END,
) -> tuple[bool, datetime]:
    """Bound "free time right now" by working hours and a cap.

    Args:
        now: The current instant as *naive UTC*.
        tz: IANA timezone name for the user's local working hours.
        cap_minutes: Never look further ahead than this (so a wide-open evening
            doesn't offer a multi-hour task).
        day_start / day_end: Local ``HH:MM`` working-hours band.

    Returns:
        ``(within_hours, horizon)``. ``within_hours`` is ``False`` when *now* is
        outside the local band (nothing should be offered); otherwise ``horizon``
        is ``min(now + cap, today's local day_end)`` as naive UTC.
    """
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    local = now.replace(tzinfo=timezone.utc).astimezone(zone)
    sh, sm = (int(x) for x in day_start.split(":"))
    eh, em = (int(x) for x in day_end.split(":"))
    start_local = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_local = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if local < start_local or local >= end_local:
        return (False, now)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    horizon = min(now + timedelta(minutes=cap_minutes), end_utc)
    return (True, horizon)


def available_now(
    commitments: list[dict[str, Any]],
    now: datetime,
    horizon: datetime,
    *,
    default_event_minutes: float = DEFAULT_EVENT_MINUTES,
) -> float:
    """Minutes free *starting right now*, until the first commitment or horizon.

    Returns ``0`` when a commitment is in progress (you're busy now) or the
    horizon is not ahead of now. Both bounds are naive UTC.
    """
    if horizon <= now:
        return 0.0
    windows = free_windows(
        commitments, now, horizon, min_minutes=0, default_event_minutes=default_event_minutes
    )
    if windows and _parse(windows[0].start) <= now:
        return windows[0].minutes
    return 0.0


#: Local hour at/after which we prefer low-energy todos ("later in the day").
DEFAULT_EVENING_AFTER_HOUR = 14
_ENERGY_DEMAND = {"low": 0, "medium": 1, "high": 2}


def local_hour_of(now: datetime, tz: str) -> int:
    """The local hour (0–23) for a naive-UTC instant, in timezone ``tz``."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return now.replace(tzinfo=timezone.utc).astimezone(zone).hour


def energy_time_rank(
    energy: str | None, local_hour: int, *, evening_after: int = DEFAULT_EVENING_AFTER_HOUR
) -> int:
    """Rank (0 = best) of a todo's energy for the time of day.

    In the afternoon we prefer low-energy tasks (higher-demand tasks rank worse,
    since willpower is thinner); in the morning energy is neutral.
    """
    if local_hour < evening_after:
        return 0
    return _ENERGY_DEMAND.get((energy or "medium").lower(), 1)


def pick_now(
    fits: list[dict[str, Any]],
    avoided_ids: list[int],
    local_hour: int,
    *,
    evening_after: int = DEFAULT_EVENING_AFTER_HOUR,
) -> dict[str, Any] | None:
    """Choose one todo from :func:`fit_todos` output for "right now".

    Honest prioritization: surface the **most-avoided** todo that fits (fighting
    the pull toward the shiny/easy thing). If nothing genuinely avoided fits, take
    the best fit but **prefer low-energy tasks later in the day**.

    Args:
        fits: :func:`fit_todos` output — ``[{todo, effective_minutes}, …]``.
        avoided_ids: Todo ids, most-avoided first (from ``avoided_todos``).
        local_hour: Current local hour (0–23).

    Returns:
        ``{todo, effective_minutes, reason}`` (``reason`` is ``"avoided"`` or
        ``"fits"``), or ``None`` when ``fits`` is empty.
    """
    if not fits:
        return None
    by_id = {f["todo"]["id"]: f for f in fits}
    for tid in avoided_ids:  # most-avoided first
        if tid in by_id:
            return {**by_id[tid], "reason": "avoided"}
    # Nothing genuinely avoided fits — best fit, low-energy-later preference.
    # sorted() is stable, so fit_todos' deadline/priority/shortest order is kept
    # within an equal energy rank.
    ordered = sorted(
        fits,
        key=lambda f: energy_time_rank(f["todo"].get("energy"), local_hour, evening_after=evening_after),
    )
    return {**ordered[0], "reason": "fits"}


def _fit_key(item: tuple[dict[str, Any], float]) -> tuple:
    """Sort key: soonest deadline, then highest priority, then quickest."""
    todo, effective = item
    has_deadline = 0 if todo.get("deadline") else 1
    deadline = todo.get("deadline") or ""
    return (has_deadline, deadline, -(todo.get("priority") or 0), effective)


def fit_todos(
    available_minutes: float, todos: list[dict[str, Any]], bias: float = 1.0
) -> list[dict[str, Any]]:
    """Rank the open todos that fit in an available block of time.

    A todo fits when ``estimate_minutes × bias <= available_minutes`` (the bias
    makes the fit honest about how long things actually take). Todos without an
    estimate are excluded — we can't promise they'll fit.

    Args:
        available_minutes: Minutes you have free.
        todos: Open todo dicts.
        bias: The ``time_estimation_bias`` multiplier.

    Returns:
        A list of ``{todo, effective_minutes}`` dicts, best candidate first.
    """
    candidates: list[tuple[dict[str, Any], float]] = []
    for todo in todos:
        estimate = todo.get("estimate_minutes")
        if estimate is None:
            continue
        effective = estimate * bias
        if effective <= available_minutes:
            candidates.append((todo, round(effective, 1)))
    candidates.sort(key=_fit_key)
    return [{"todo": t, "effective_minutes": eff} for t, eff in candidates]


def suggest_for_windows(
    windows: list[FreeWindow], todos: list[dict[str, Any]], bias: float = 1.0
) -> list[dict[str, Any]]:
    """Propose one todo per free window (no todo suggested twice).

    Greedy, chronological: each window takes the best-fitting unused todo.

    Args:
        windows: Free windows (chronological).
        todos: Open todo dicts.
        bias: The time-estimation multiplier.

    Returns:
        A list of ``{window, suggestion}`` dicts (``suggestion`` may be ``None``).
    """
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for window in windows:
        remaining = [t for t in todos if t["id"] not in used]
        fits = fit_todos(window.minutes, remaining, bias)
        pick = fits[0]["todo"] if fits else None
        if pick is not None:
            used.add(pick["id"])
        out.append({"window": window, "suggestion": pick})
    return out
