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

import re
from dataclasses import dataclass, field
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


def local_datetime(now: datetime, tz: str) -> datetime:
    """A naive-UTC instant as a tz-aware local datetime (falls back to UTC)."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return now.replace(tzinfo=timezone.utc).astimezone(zone)


def local_hour_of(now: datetime, tz: str) -> int:
    """The local hour (0–23) for a naive-UTC instant, in timezone ``tz``."""
    return local_datetime(now, tz).hour


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
        key=lambda f: energy_time_rank(
            f["todo"].get("energy"), local_hour, evening_after=evening_after
        ),
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


# --- Suggestion time windows (per-category / per-source, hard off-zone) -------
#
# Not every open todo belongs in every gap. "Make coffee" is fine any waking
# hour; "prep the board deck" belongs in focus hours; nothing at all belongs at
# 3am. So a todo is *suggestible* at a given local time only when that time falls
# inside the todo's resolved window AND outside a hard global off-zone.
#
# The window is resolved by precedence — a per-todo override, then the todo's
# category, then its source, then a permissive default (the full waking band, so
# an uncategorized "make coffee" is offerable all day). The off-zone is an outer
# bound applied to *everything*: even a 24h window never surfaces inside it.
#
# Windows are local wall-clock ranges "HH:MM-HH:MM"; a range whose start is after
# its end (e.g. the 22:00-06:00 off-zone) wraps midnight. Pure and config-driven:
# the endpoints/briefing build a WindowConfig from settings + coaching-state and
# pass it in.

#: Global off-zone (local): nothing is ever suggested inside ``[start, end)``.
DEFAULT_OFFZONE = "22:00-06:00"
#: Window for a todo whose category/source isn't configured — the full waking
#: band (the off-zone's complement), so uncategorized "anytime" todos are always
#: offerable. Only categories with a narrower default (below) constrain further.
DEFAULT_TODO_WINDOW = "06:00-22:00"
#: Built-in windows for the categories :mod:`prefrontal.todos` infers. Operators
#: override any of these (and add source keys) via config; unlisted categories
#: fall back to :data:`DEFAULT_TODO_WINDOW`.
DEFAULT_CATEGORY_WINDOWS: dict[str, str] = {
    "work": "09:00-17:00",          # focus / business hours
    "finance": "09:00-18:00",       # banks, billers, business hours
    "admin": "09:00-18:00",         # calls, forms, offices open
    "communication": "08:00-21:00",  # civil hours to reach people
    "errands": "09:00-20:00",       # shops open
    "learning": "07:00-22:00",      # early or late, but not overnight
    "health": "06:00-21:00",        # early workout or evening
    "home": "06:00-22:00",          # any waking hour you're home
}


def _hhmm_to_minutes(value: str) -> int | None:
    """Parse ``"HH:MM"`` into minutes-from-midnight (0–1439), or ``None``."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m:
        return None
    hours, mins = int(m.group(1)), int(m.group(2))
    if not (0 <= hours <= 23 and 0 <= mins <= 59):
        return None
    return hours * 60 + mins


def _minutes_to_hhmm(minute: int) -> str:
    """Format minutes-from-midnight as ``"HH:MM"`` (wrapping at 24h)."""
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_window(spec: str | None) -> tuple[int, int] | None:
    """Parse a ``"HH:MM-HH:MM"`` window into ``(start_min, end_min)``, or ``None``.

    A malformed spec (bad shape, out-of-range clock, or equal endpoints) yields
    ``None`` so callers can fall back rather than raise. ``start > end`` is a
    legal midnight-wrapping window (e.g. ``"22:00-06:00"``).
    """
    if not spec:
        return None
    parts = spec.split("-")
    if len(parts) != 2:
        return None
    start, end = _hhmm_to_minutes(parts[0]), _hhmm_to_minutes(parts[1])
    if start is None or end is None or start == end:
        return None
    return (start, end)


def _in_span(minute: int, span: tuple[int, int]) -> bool:
    """Whether ``minute`` (0–1439) is inside ``[start, end)``, midnight-wrap aware."""
    start, end = span
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


@dataclass(frozen=True)
class WindowConfig:
    """Resolved suggestion-window policy: off-zone, per-key windows, default.

    All windows are ``(start_min, end_min)`` local, midnight-wrap aware. Build
    via :meth:`build`, which layers coaching-state over env over built-ins.

    Attributes:
        offzone: The hard global off-zone; nothing is suggested inside it.
        windows: Key → window, where a key is a todo ``category`` or ``source``.
        default_window: Fallback window when no key matches.
    """

    offzone: tuple[int, int]
    default_window: tuple[int, int]
    windows: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        env_offzone: str | None = None,
        env_windows: dict[str, str] | None = None,
        state_offzone: str | None = None,
        state_windows: dict[str, str] | None = None,
        default_window: str | None = None,
    ) -> WindowConfig:
        """Assemble a config, layering **state over env over built-in** per key.

        Every ``*_window`` string is ``"HH:MM-HH:MM"``; an unparseable value is
        ignored (the lower-precedence value stands), so a typo degrades to the
        default rather than raising. ``env_windows`` / ``state_windows`` map a
        category or source key to its window.
        """
        offzone = (
            parse_window(state_offzone)
            or parse_window(env_offzone)
            or parse_window(DEFAULT_OFFZONE)
        )
        default = (
            parse_window(default_window)
            or parse_window(DEFAULT_TODO_WINDOW)
        )
        windows: dict[str, tuple[int, int]] = {}
        for key, spec in DEFAULT_CATEGORY_WINDOWS.items():
            parsed = parse_window(spec)
            if parsed is not None:
                windows[key] = parsed
        for layer in (env_windows or {}, state_windows or {}):
            for key, spec in layer.items():
                parsed = parse_window(spec)
                if parsed is not None:
                    windows[key.strip().lower()] = parsed
        assert offzone is not None and default is not None  # built-in constants parse
        return cls(offzone=offzone, default_window=default, windows=windows)

    def awake_band(self) -> tuple[str, str]:
        """The off-zone's complement as ``("HH:MM", "HH:MM")`` waking-hours band.

        The outer gate for "what fits right now": the widget offers nothing
        outside it. For the usual ``22:00-06:00`` off-zone this is
        ``("06:00", "22:00")``.
        """
        off_start, off_end = self.offzone
        return (_minutes_to_hhmm(off_end), _minutes_to_hhmm(off_start))


#: Coaching-state key for a per-user off-zone override ("HH:MM-HH:MM").
STATE_OFFZONE_KEY = "todo_offzone"
#: Prefix for per-user category/source window overrides in coaching-state, e.g.
#: ``"todo_window:work" -> "09:00-17:00"``.
STATE_WINDOW_PREFIX = "todo_window:"


def window_config_for(settings: Any, store: Any) -> WindowConfig:
    """Build the active :class:`WindowConfig` from settings + coaching-state.

    Layers per-user coaching-state over the operator's env config over the
    built-in defaults (see :meth:`WindowConfig.build`). Duck-typed: ``settings``
    supplies ``todo_offzone``/``todo_window_map`` and ``store`` supplies
    ``all_state()`` (``{key: {"value": ...}}``), so this stays free of hard
    ``config``/``store`` imports.

    Args:
        settings: A :class:`~prefrontal.config.Settings`-like object.
        store: A :class:`~prefrontal.memory.store.MemoryStore`-like object.

    Returns:
        The resolved window policy.
    """
    state = store.all_state() or {}

    def sval(key: str) -> str | None:
        entry = state.get(key)
        value = entry.get("value") if isinstance(entry, dict) else entry
        return value if isinstance(value, str) else None

    state_windows = {
        key[len(STATE_WINDOW_PREFIX):]: sval(key)
        for key in state
        if key.startswith(STATE_WINDOW_PREFIX)
    }
    return WindowConfig.build(
        env_offzone=getattr(settings, "todo_offzone", "") or None,
        env_windows=dict(getattr(settings, "todo_window_map", {}) or {}),
        state_offzone=sval(STATE_OFFZONE_KEY),
        state_windows=state_windows,
    )


def resolve_window(todo: dict[str, Any], config: WindowConfig) -> tuple[int, int]:
    """The window governing ``todo``: per-todo override → category → source → default.

    The per-todo override is the todo's ``time_window`` field (a ``"HH:MM-HH:MM"``
    string); an unparseable one is ignored. Category is tried before source, and
    both look up the same :attr:`WindowConfig.windows` map.
    """
    override = parse_window(todo.get("time_window"))
    if override is not None:
        return override
    category = (todo.get("category") or "").strip().lower()
    if category in config.windows:
        return config.windows[category]
    source = (todo.get("source") or "").strip().lower()
    if source in config.windows:
        return config.windows[source]
    return config.default_window


def todo_allowed_at(
    todo: dict[str, Any], local_dt: datetime, config: WindowConfig
) -> bool:
    """Whether ``todo`` is suggestible at local wall-clock time ``local_dt``.

    ``True`` only when ``local_dt`` is **outside** the off-zone **and inside** the
    todo's resolved window (see :func:`resolve_window`). ``local_dt`` is read for
    its hour/minute only, so a naive-local or tz-aware datetime both work.
    """
    minute = local_dt.hour * 60 + local_dt.minute
    if _in_span(minute, config.offzone):
        return False
    return _in_span(minute, resolve_window(todo, config))


def filter_suggestible(
    todos: list[dict[str, Any]], local_dt: datetime, config: WindowConfig
) -> list[dict[str, Any]]:
    """The subset of ``todos`` suggestible at ``local_dt`` (order preserved)."""
    return [t for t in todos if todo_allowed_at(t, local_dt, config)]


def _window_local_dt(utc_start: str, tz: str) -> datetime:
    """Local wall-clock datetime for a free window's naive-UTC start."""
    return local_datetime(_parse(utc_start), tz)


def suggest_for_windows(
    windows: list[FreeWindow],
    todos: list[dict[str, Any]],
    bias: float = 1.0,
    *,
    config: WindowConfig | None = None,
    tz: str | None = None,
) -> list[dict[str, Any]]:
    """Propose one todo per free window (no todo suggested twice).

    Greedy, chronological: each window takes the best-fitting unused todo. When
    ``config`` and ``tz`` are both given, a todo is only eligible for a window if
    it's suggestible at that window's *local* start time (:func:`todo_allowed_at`)
    — so a focus-hours task isn't proposed for an 8pm gap, and nothing is proposed
    inside the off-zone. Omit them to rank purely by fit (legacy behavior).

    Args:
        windows: Free windows (chronological).
        todos: Open todo dicts.
        bias: The time-estimation multiplier.
        config: Optional suggestion-window policy.
        tz: IANA timezone for interpreting each window's local time (required for
            ``config`` to take effect).

    Returns:
        A list of ``{window, suggestion}`` dicts (``suggestion`` may be ``None``).
    """
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for window in windows:
        remaining = [t for t in todos if t["id"] not in used]
        if config is not None and tz is not None:
            remaining = filter_suggestible(
                remaining, _window_local_dt(window.start, tz), config
            )
        fits = fit_todos(window.minutes, remaining, bias)
        pick = fits[0]["todo"] if fits else None
        if pick is not None:
            used.add(pick["id"])
        out.append({"window": window, "suggestion": pick})
    return out
