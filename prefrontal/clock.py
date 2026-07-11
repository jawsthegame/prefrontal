"""Canonical clock and timestamp wire-format for Prefrontal.

Prefrontal stores every timestamp as **naive UTC** text in one format —
``YYYY-MM-DD HH:MM:SS`` — the de-facto contract between the SQLite layer and
every domain module. This module is the single source of truth for that
contract, so callers never re-inline the format literal or re-derive the
truncate-to-19-chars parse (both of which had drifted into ~13 private copies).

- :func:`utcnow` — "now" as the stored representation (naive UTC, second precision).
- :func:`fmt_ts` — a datetime → stored text.
- :func:`parse_ts` — stored text → datetime, tolerant of ``None``/garbage (→ ``None``).
- :func:`parse_ts_strict` — the same parse for trusted stored values, raising on
  malformed input rather than masking corruption as ``None``.

Timezone conversion (naive-UTC → a local wall clock) also lives here — the
``local_*`` family (:func:`local_datetime`, :func:`local_hour_of`,
:func:`local_day_bounds`, :func:`local_time_utc`, :func:`end_of_local_day_utc`) —
so the single source of truth for the time contract owns *both* halves: the
stored-UTC representation and its projection onto a user's local wall clock.
(These lived in :mod:`prefrontal.scheduling` until that made the memory layer
import a domain module just to read a local hour; ``clock`` is the leaf they
belong in.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The one on-disk timestamp format: naive UTC, second precision.
TS_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    """Current time as naive UTC (second precision), matching stored timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def fmt_ts(dt: datetime) -> str:
    """Format a datetime as a stored naive-UTC timestamp (:data:`TS_FMT`)."""
    return dt.strftime(TS_FMT)


def parse_ts(ts: object) -> datetime | None:
    """Parse a stored naive-UTC timestamp, tolerant of ``None``/garbage.

    Truncates to the first 19 chars (dropping any sub-second/zone suffix) and
    returns ``None`` on anything unparseable — the right default for reading
    cursors and episode rows that may be absent or malformed.
    """
    try:
        return datetime.strptime(str(ts)[:19], TS_FMT)
    except (ValueError, TypeError):
        return None


def parse_ts_strict(ts: str) -> datetime:
    """Parse a stored naive-UTC timestamp, raising on malformed input.

    For trusted stored values (a commitment's ``start_at``, an outing's
    ``departed_at``) where a parse failure means corruption worth surfacing, not
    a ``None`` to swallow.
    """
    return datetime.strptime(str(ts)[:19], TS_FMT)


def parse_hour(raw: object, default: int) -> int:
    """Parse a coaching-state "clock hour" value, tolerant of ``None``/garbage.

    An hour preference (``responsive_hours_start``, ``meal_start_hour`` …) is a
    bare hour of the day. It's *meant* to be stored as a plain integer
    (``"8"``, ``"14"``), but a time-picker UI or a hand edit readily writes an
    ``HH:MM`` / ``HH:MM:SS`` clock string (``"08:00"``) instead — which
    ``float("08:00")`` cannot parse, so a naive ``int(get_float(...))`` silently
    fell back to the default and quietly ignored the user's window. This accepts
    both forms: it takes the hour component before the first ``:`` and drops the
    minutes (the windows are hour-granular). A missing, empty, out-of-range
    (not 0–23), or otherwise unparseable value falls back to ``default`` rather
    than raising, mirroring :meth:`~prefrontal.memory.repos.state.StateRepo.get_float`.
    """
    if raw is None:
        return default
    head = str(raw).strip().split(":", 1)[0]
    if not head:
        return default
    try:
        hour = int(head)
    except (TypeError, ValueError):
        return default
    return hour if 0 <= hour <= 23 else default


# --- Local wall-clock projection (naive UTC → the user's zone) ----------------


def local_datetime(now: datetime, tz: str) -> datetime:
    """A naive-UTC instant as a tz-aware local datetime (falls back to UTC)."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return now.replace(tzinfo=timezone.utc).astimezone(zone)


def local_hour_of(now: datetime, tz: str) -> int:
    """The local hour (0–23) for a naive-UTC instant, in timezone ``tz``."""
    return local_datetime(now, tz).hour


def local_day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """Naive-UTC ``[start, end)`` of the *local* calendar day containing ``now``.

    "Today" is a local notion: for an Eastern user at 9pm, midnight-to-midnight is
    01:00–01:00 UTC spanning two UTC dates — not ``00:00`` UTC. Every surface that
    scopes to "today" (the briefing, panic, the rough-day assessment, the cascade)
    should bound its query with this rather than ``now.replace(hour=0, …)`` on the
    naive-UTC instant, which silently rolls the day over hours early or late.

    Args:
        now: The current instant as naive UTC.
        tz: IANA timezone name for the local day (falls back to UTC if unknown).

    Returns:
        ``(day_start, day_end)`` as naive UTC — the ``end`` is the next local
        midnight (exclusive), ready to pass to ``commitments_between`` /
        ``episodes_since``. DST-correct: each bound carries the offset in force at
        that wall-clock instant.
    """
    local = local_datetime(now, tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    def _to_naive_utc(dt: datetime) -> datetime:
        return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)

    return _to_naive_utc(start_local), _to_naive_utc(end_local)


def local_time_utc(now: datetime, tz: str, hour: int, minute: int = 0) -> datetime:
    """Naive-UTC instant of local ``hour:minute`` on the local day containing ``now``.

    The building block for a *local* working-hours band (e.g. "8am–8pm your
    time"): ``day_start.replace(hour=8)`` on a naive-UTC instant yields 8am **UTC**,
    which for an Eastern user is 3am local — so the briefing's spare-time band and
    the rough-day re-fit anchor their hours in the local zone and convert back.
    DST-correct via :func:`local_datetime`.
    """
    local = local_datetime(now, tz).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def end_of_local_day_utc(day: datetime, tz: str) -> datetime:
    """Naive-UTC instant of 23:59:59 *local* on the calendar date of ``day``.

    A date-only deadline ("due 2026-07-07") means "by the end of that day in
    *your* zone". Anchoring it to 23:59:59 UTC flags a western-hemisphere user's
    "due today" todo as overdue hours early — at 8pm Eastern the clock is already
    past a 23:59 UTC cutoff. Only ``day``'s Y-M-D (the local date) is read; its
    time component is ignored. Falls back to UTC when ``tz`` is unknown.
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = day.replace(
        hour=23, minute=59, second=59, microsecond=0, tzinfo=zone
    )
    return local.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
