"""Tests for the timezone-aware day/band helpers in :mod:`prefrontal.scheduling`.

These are the primitives every "today" surface (briefing, panic, encouragement,
the cascade) uses so the day boundary follows the user's *local* calendar day
rather than the UTC day — the difference that made a 9pm-Eastern glance show
tomorrow-morning's commitments as "behind".
"""

from __future__ import annotations

from datetime import datetime

from prefrontal.scheduling import local_day_bounds, local_time_utc

# July → EDT (UTC-4) for America/New_York, so the arithmetic below is offset −4.


def test_local_day_bounds_eastern_evening_spans_two_utc_dates():
    # 2026-07-07 01:00 UTC is 2026-07-06 21:00 EDT — still the local 6th. Its day
    # runs local midnight-to-midnight = 04:00 UTC (6th) → 04:00 UTC (7th).
    now = datetime(2026, 7, 7, 1, 0, 0)
    start, end = local_day_bounds(now, "America/New_York")
    assert start == datetime(2026, 7, 6, 4, 0, 0)
    assert end == datetime(2026, 7, 7, 4, 0, 0)


def test_local_day_bounds_utc_is_the_calendar_day():
    now = datetime(2026, 7, 6, 23, 30, 0)
    assert local_day_bounds(now, "UTC") == (
        datetime(2026, 7, 6, 0, 0, 0),
        datetime(2026, 7, 7, 0, 0, 0),
    )


def test_local_day_bounds_unknown_zone_falls_back_to_utc():
    now = datetime(2026, 7, 6, 23, 30, 0)
    assert local_day_bounds(now, "Not/AZone") == local_day_bounds(now, "UTC")


def test_local_time_utc_anchors_local_hour():
    # 8am EDT on the local day of `now` is 12:00 UTC.
    now = datetime(2026, 7, 6, 15, 0, 0)  # 11:00 EDT on the 6th
    assert local_time_utc(now, "America/New_York", 8) == datetime(2026, 7, 6, 12, 0, 0)
    assert local_time_utc(now, "UTC", 8) == datetime(2026, 7, 6, 8, 0, 0)
