"""Tests for the canonical clock / timestamp wire-format (prefrontal.clock)."""
from __future__ import annotations

from datetime import datetime

import pytest

from prefrontal.clock import (
    TS_FMT,
    fmt_ts,
    parse_hour,
    parse_ts,
    parse_ts_strict,
    utcnow,
)


def test_utcnow_is_naive_utc_second_precision():
    now = utcnow()
    assert now.tzinfo is None  # naive, matching stored timestamps
    assert now.microsecond == 0


def test_fmt_parse_round_trip():
    dt = datetime(2026, 7, 3, 14, 30, 5)
    text = fmt_ts(dt)
    assert text == "2026-07-03 14:30:05"
    assert parse_ts(text) == dt
    assert parse_ts_strict(text) == dt


def test_parse_ts_truncates_subsecond_and_zone_suffix():
    # A stored value may carry a fractional/zone tail; we parse the first 19 chars.
    assert parse_ts("2026-07-03 14:30:05.123456") == datetime(2026, 7, 3, 14, 30, 5)
    assert parse_ts("2026-07-03 14:30:05+00:00") == datetime(2026, 7, 3, 14, 30, 5)


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-99 99:99:99", 12345])
def test_parse_ts_is_tolerant(bad):
    assert parse_ts(bad) is None


def test_parse_ts_strict_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_ts_strict("not-a-date")


def test_ts_fmt_is_the_documented_format():
    assert TS_FMT == "%Y-%m-%d %H:%M:%S"


def test_parse_hour_accepts_bare_hours():
    assert parse_hour("8", 22) == 8
    assert parse_hour("0", 22) == 0
    assert parse_hour("23", 22) == 23
    assert parse_hour(" 14 ", 22) == 14  # whitespace tolerated


def test_parse_hour_accepts_hh_mm_clock_strings():
    # The regression: a time-picker / hand edit writes "HH:MM", which
    # float("08:00") could not parse — so the window was silently dropped.
    assert parse_hour("08:00", 22) == 8
    assert parse_hour("14:00", 22) == 14
    assert parse_hour("14:30:00", 22) == 14  # minutes/seconds dropped (hour-granular)


@pytest.mark.parametrize("bad", [None, "", "  ", "not-an-hour", "24", "-1", "99:00", 3.5])
def test_parse_hour_falls_back_on_garbage_or_out_of_range(bad):
    assert parse_hour(bad, 22) == 22
