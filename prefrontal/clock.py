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

Timezone conversion (naive-UTC → a local wall clock) lives in
:mod:`prefrontal.scheduling` (``local_datetime`` / ``local_hour_of``), which is
its natural home alongside the day-window logic that consumes it.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
