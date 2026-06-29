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
from datetime import datetime, timedelta
from typing import Any

#: Assumed duration for a commitment with no ``end_at`` when carving windows.
DEFAULT_EVENT_MINUTES = 30.0
#: Ignore gaps shorter than this — not worth surfacing.
DEFAULT_MIN_WINDOW_MINUTES = 10.0


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
