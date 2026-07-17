"""Tests for the timezone-aware day/band helpers in :mod:`prefrontal.scheduling`.

These are the primitives every "today" surface (briefing, panic, encouragement,
the cascade) uses so the day boundary follows the user's *local* calendar day
rather than the UTC day — the difference that made a 9pm-Eastern glance show
tomorrow-morning's commitments as "behind".
"""

from __future__ import annotations

from datetime import datetime

from prefrontal.clock import end_of_local_day_utc, local_day_bounds, local_time_utc
from prefrontal.scheduling import (
    find_slots,
    fit_todos,
)


def _todo(tid, *, minutes=15, priority=1, deadline=None, project_rank=None):
    """Minimal open-todo dict for the fit ranker."""
    return {
        "id": tid, "title": f"todo {tid}", "estimate_minutes": minutes,
        "priority": priority, "deadline": deadline, "project_rank": project_rank,
    }


def _order(fits):
    return [f["todo"]["id"] for f in fits]


def test_fit_project_rank_breaks_ties_after_priority():
    # Same deadline (none) and same priority: the todo in the higher-priority
    # project (lower rank) comes first.
    fits = fit_todos(60, [
        _todo(1, project_rank=3),
        _todo(2, project_rank=1),
        _todo(3, project_rank=2),
    ])
    assert _order(fits) == [2, 3, 1]


def test_fit_todo_priority_still_outranks_project_rank():
    # A higher todo priority wins even from a lower-priority project — rank is only
    # a tiebreak *after* priority (soft influence).
    fits = fit_todos(60, [
        _todo(1, priority=1, project_rank=1),  # top project, normal priority
        _todo(2, priority=3, project_rank=9),  # bottom project, urgent
    ])
    assert _order(fits) == [2, 1]


def test_fit_deadline_still_wins_over_project_rank():
    # A near deadline in a low-priority project still surfaces first.
    fits = fit_todos(60, [
        _todo(1, project_rank=1, deadline=None),
        _todo(2, project_rank=9, deadline="2026-07-11 09:00:00"),
    ])
    assert _order(fits) == [2, 1]


def test_fit_orphan_only_loses_on_exact_tie():
    # A project-less todo (rank None) is judged purely on deadline+priority; it only
    # yields to a ranked-project todo on an exact deadline+priority tie.
    tie = fit_todos(60, [_todo(1, project_rank=None), _todo(2, project_rank=5)])
    assert _order(tie) == [2, 1]  # exact tie → ranked project edges out the orphan
    # But a higher-priority orphan still beats a top-project todo.
    win = fit_todos(
        60, [_todo(1, priority=2, project_rank=None), _todo(2, priority=1, project_rank=1)]
    )
    assert _order(win) == [1, 2]


def _commit(start_at, end_at=None):
    """Minimal commitment dict for the slot/window helpers."""
    return {"start_at": start_at, "end_at": end_at}

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


def test_end_of_local_day_utc():
    # End of 2026-07-07 EDT (UTC-4) is 2026-07-08 03:59:59 UTC — so a "due today"
    # todo isn't overdue until then, not at 23:59 UTC (7:59pm Eastern).
    assert end_of_local_day_utc(datetime(2026, 7, 7), "America/New_York") == datetime(
        2026, 7, 8, 3, 59, 59
    )
    assert end_of_local_day_utc(datetime(2026, 7, 7), "UTC") == datetime(
        2026, 7, 7, 23, 59, 59
    )


def test_parse_deadline_anchors_to_local_end_of_day(monkeypatch):
    """A date-only deadline resolves to the end of that *local* day (not UTC)."""
    from prefrontal import panic, todos
    from prefrontal.config import Settings

    est = Settings(webhook_secret="x", timezone="America/New_York")
    # todos._parse_deadline lazily imports get_settings from prefrontal.config;
    # panic._parse_deadline binds it at its own module scope.
    monkeypatch.setattr("prefrontal.config.get_settings", lambda: est)
    monkeypatch.setattr(panic, "get_settings", lambda: est)

    expected = datetime(2026, 7, 8, 3, 59, 59)  # end of 7/7 EDT, in UTC
    assert todos._parse_deadline("2026-07-07") == expected
    assert todos._parse_deadline("2026-07-07 00:00:00") == expected
    assert panic._parse_deadline("2026-07-07") == expected


# ── find_slots: free windows of a specific length across the coming days ──────


def test_find_slots_returns_gaps_at_least_minutes_long():
    # UTC keeps the arithmetic literal. A single 10–11 meeting today.
    now = datetime(2026, 7, 7, 9, 0, 0)
    commitments = [_commit("2026-07-07 10:00:00", "2026-07-07 11:00:00")]
    slots = find_slots(commitments, now, "UTC", minutes=30, days=1)
    # 09:00→10:00 (clamped to now) and 11:00→22:00 (band end) both qualify.
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-07 09:00:00", "2026-07-07 10:00:00"),
        ("2026-07-07 11:00:00", "2026-07-07 22:00:00"),
    ]
    assert slots[0].minutes == 60.0


def test_find_slots_drops_gaps_shorter_than_requested():
    now = datetime(2026, 7, 7, 9, 0, 0)
    commitments = [_commit("2026-07-07 10:00:00", "2026-07-07 11:00:00")]
    # The 09:00–10:00 gap is only 60 min, so a 90-min ask skips it.
    slots = find_slots(commitments, now, "UTC", minutes=90, days=1)
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-07 11:00:00", "2026-07-07 22:00:00"),
    ]


def test_find_slots_never_offers_overnight_and_spans_days():
    # No commitments, two days: each day contributes only its waking band, so a
    # slot never runs through the night.
    now = datetime(2026, 7, 7, 9, 0, 0)
    slots = find_slots([], now, "UTC", minutes=60, days=2)
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-07 09:00:00", "2026-07-07 22:00:00"),   # today, clamped to now
        ("2026-07-08 06:00:00", "2026-07-08 22:00:00"),   # tomorrow's full band
    ]


def test_find_slots_today_clamped_to_now():
    now = datetime(2026, 7, 7, 14, 30, 0)
    slots = find_slots([], now, "UTC", minutes=30, days=1)
    assert slots[0].start == "2026-07-07 14:30:00"


def test_find_slots_waking_band_is_local():
    # 12:00 UTC is 08:00 EDT. The 06:00–22:00 *local* band ends at 22:00 EDT =
    # 02:00 UTC next day, so today's single slot runs now→02:00 UTC.
    now = datetime(2026, 7, 7, 12, 0, 0)
    slots = find_slots([], now, "America/New_York", minutes=60, days=1)
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-07 12:00:00", "2026-07-08 02:00:00"),
    ]


def test_find_slots_custom_awake_band():
    now = datetime(2026, 7, 7, 6, 0, 0)
    slots = find_slots([], now, "UTC", minutes=30, days=1, awake_band=("09:00", "17:00"))
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-07 09:00:00", "2026-07-07 17:00:00"),
    ]


def test_find_slots_respects_limit():
    now = datetime(2026, 7, 7, 9, 0, 0)
    slots = find_slots([], now, "UTC", minutes=30, days=10, limit=3)
    assert len(slots) == 3


def test_find_slots_rejects_nonpositive():
    now = datetime(2026, 7, 7, 9, 0, 0)
    assert find_slots([], now, "UTC", minutes=0, days=1) == []
    assert find_slots([], now, "UTC", minutes=30, days=0) == []


def test_suggest_now_ignores_fyi_and_hold_blocks():
    """FYI events and placeholder/hold blocks don't consume your free time.

    Regression: a genuinely-free user whose only calendar entry right now is
    someone else's event (an FYI, e.g. a kid's recital) or a movable focus/hold
    block must still get a suggestion — not "no free time right now". Only real,
    own commitments (:func:`prefrontal.commitments.is_attendable`) block.
    """
    from prefrontal.config import Settings
    from prefrontal.memory.store import MemoryStore
    from prefrontal.scheduling import suggest_now
    from tests.conftest import scoped_default

    settings = Settings(webhook_secret="s", timezone="UTC")
    now = datetime(2026, 7, 7, 13, 0, 0)  # midday, inside waking hours
    span = ("2026-07-07 13:00:00", "2026-07-07 14:00:00")  # covers "now"

    # A hold is a normal-kind ("self") commitment whose *title* is a placeholder.
    for kind, title, eid in [("fyi", "Kid's recital", "fyi:1"), ("self", "Focus", "hold:1")]:
        with MemoryStore.open(":memory:") as raw:
            store = scoped_default(raw)
            store.add_todo("Quick email", estimate_minutes=15, priority=2)
            store.upsert_commitment(
                title=title, start_at=span[0], end_at=span[1],
                external_id=eid, kind=kind,
            )
            result = suggest_now(store, settings, now)
            assert result["free_minutes"] > 0, f"{title} wrongly consumed free time"
            assert result["suggestion"] is not None, f"{title} suppressed the suggestion"
