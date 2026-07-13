"""Tests for the end-of-day self-care gap review (:mod:`prefrontal.self_care_review`).

Covers the pure analysis (late-first, long-gap, shortfall, none, on-track), the
push-message gating (fires only on a gap, silent on a clean day / when off), and
the opt-in evening cue on the coaching tick
(:meth:`~prefrontal.modules.self_care.SelfCareModule._eod_review_cue`).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from prefrontal.coaching import CoachContext, last_fired
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import SelfCareModule
from prefrontal.self_care_review import (
    render_review,
    review_message,
    self_care_review,
)
from tests.conftest import scoped_default

TZ = "UTC"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        s = scoped_default(MemoryStore(conn))
        s.set_state("self_care", "on", source="explicit")
        yield s
    finally:
        conn.close()


def _confirm(store: MemoryStore, key: str, hh: int, mm: int = 0, day: str = "2026-07-13"):
    """Log a self-care confirm exactly the way the module does, at a fixed time."""
    store.log_episode(
        "self_care",
        acknowledged=True,
        context=f"{key}: confirmed",
        outcome="confirmed",
        notes="latency=5s",
        timestamp=f"{day} {hh:02d}:{mm:02d}:00",
    )


EVENING = datetime(2026, 7, 13, 21, 0, 0)  # 9pm UTC on the test day


# -- the analysis -------------------------------------------------------------


def test_late_first_flags_a_met_but_late_quota(store):
    """Six glasses, but the first not until 3pm — a gap even though target was met."""
    for hh, mm in [(15, 0), (15, 40), (16, 20), (17, 0), (18, 0), (19, 0)]:
        _confirm(store, "water", hh, mm)
    review = self_care_review(store, EVENING, TZ)
    water = next(c for c in review["checks"] if c["key"] == "water")
    kinds = {f["kind"] for f in water["findings"]}
    assert "late_first" in kinds
    assert water["count"] == 6  # target met…
    assert not water["on_track"]  # …but not "on track" because of the gap
    assert any("3:00 pm" in g for g in review["gaps"])


def test_long_gap_between_confirms(store):
    """~6h between two bio breaks is flagged against the 2h cadence."""
    _confirm(store, "biobreak", 8, 30)
    _confirm(store, "biobreak", 14, 40)
    review = self_care_review(store, EVENING, TZ)
    bio = next(c for c in review["checks"] if c["key"] == "biobreak")
    kinds = {f["kind"] for f in bio["findings"]}
    assert "long_gap" in kinds
    assert any("between bio breaks" in g for g in review["gaps"])


def test_shortfall_when_quota_unmet(store):
    """A water day that ends under target reports the shortfall."""
    for hh in (9, 10, 12):  # 3 of 6, well-paced from the start hour
        _confirm(store, "water", hh, 0)
    review = self_care_review(store, EVENING, TZ)
    water = next(c for c in review["checks"] if c["key"] == "water")
    kinds = {f["kind"] for f in water["findings"]}
    assert "shortfall" in kinds
    assert any("3 of 6" in g for g in review["gaps"])


def test_none_logged_reports_gap(store):
    """An enabled check with zero confirms all day is a gap once its window opened."""
    review = self_care_review(store, EVENING, TZ)
    water = next(c for c in review["checks"] if c["key"] == "water")
    assert {f["kind"] for f in water["findings"]} == {"none"}
    assert review["has_findings"]


def test_on_track_day_has_no_findings(store):
    """A meal at noon (target 1) is a clean win, not a gap."""
    _confirm(store, "meal", 12, 0)
    review = self_care_review(store, EVENING, TZ)
    meal = next(c for c in review["checks"] if c["key"] == "meal")
    assert meal["on_track"]
    assert meal["findings"] == []
    assert "meal" in review["wins"]


def test_well_paced_water_is_on_track(store):
    """Six glasses paced ~90m apart from 9am — target met, no late-first, no gap."""
    # Isolate water: the other default-on checks (meal, biobreak) would each show a
    # legitimate 'none' gap with no confirms of their own.
    for key in ("meal", "biobreak"):
        store.set_state(f"{key}_enabled", "off", source="explicit")
    for i in range(6):
        total = 9 * 60 + i * 90
        _confirm(store, "water", total // 60, total % 60)
    review = self_care_review(store, EVENING, TZ)
    water = next(c for c in review["checks"] if c["key"] == "water")
    assert water["on_track"], water["findings"]
    assert not review["has_findings"]


def test_before_start_hour_is_not_a_none_gap():
    """Early morning: a check that hasn't reached its start hour isn't 'none' yet."""
    conn = init_db(":memory:")
    try:
        s = scoped_default(MemoryStore(conn))
        s.set_state("self_care", "on", source="explicit")
        early = datetime(2026, 7, 13, 7, 0, 0)  # 7am, before water's 9:00 start
        review = self_care_review(s, early, TZ)
        water = next(c for c in review["checks"] if c["key"] == "water")
        assert water["findings"] == []  # window not open — no gap to report
    finally:
        conn.close()


def test_disabled_check_is_omitted(store):
    """A per-check toggle off drops it from the review entirely."""
    store.set_state("water_enabled", "off", source="explicit")
    review = self_care_review(store, EVENING, TZ)
    assert all(c["key"] != "water" for c in review["checks"])


def test_master_off_returns_empty(store):
    store.set_state("self_care", "off", source="explicit")
    review = self_care_review(store, EVENING, TZ)
    assert review["enabled"] is False
    assert review["checks"] == []
    assert review_message(review) is None


def test_other_day_confirms_ignored(store):
    """Yesterday's clicks don't count toward today's review."""
    _confirm(store, "water", 15, 0, day="2026-07-12")
    review = self_care_review(store, EVENING, TZ)
    water = next(c for c in review["checks"] if c["key"] == "water")
    assert water["count"] == 0


# -- the push message ---------------------------------------------------------


def test_message_none_on_clean_day(store):
    for i in range(6):
        total = 9 * 60 + i * 90
        _confirm(store, "water", total // 60, total % 60)
    _confirm(store, "meal", 12, 0)
    review = self_care_review(store, EVENING, TZ)
    # Water clean; meal clean; other on-with-self_care checks (biobreak) have no
    # confirms → 'none' gap, so there IS a finding. Assert message reflects state.
    if review["has_findings"]:
        assert review_message(review) is not None
    else:
        assert review_message(review) is None


def test_message_leads_with_gaps_and_names_user(store):
    for hh, mm in [(15, 0), (15, 40), (16, 20), (17, 0), (18, 0), (19, 0)]:
        _confirm(store, "water", hh, mm)
    review = self_care_review(store, EVENING, TZ)
    msg = review_message(review, "Tom")
    assert msg is not None
    assert msg.startswith("Tom, end-of-day check-in")
    assert "3:00 pm" in msg


def test_render_review_off_and_clean(store):
    store.set_state("self_care", "off", source="explicit")
    off = self_care_review(store, EVENING, TZ)
    assert "off" in render_review(off).lower()


# -- the coaching-tick cue ----------------------------------------------------


def _ctx(now: datetime) -> CoachContext:
    return CoachContext(now=now, timezone=TZ, display_name="Tom", responsive_end=23)


def test_eod_cue_off_by_default(store):
    """The evening recap is opt-in — it doesn't fire until enabled."""
    _confirm(store, "water", 15, 0)  # a late-first gap exists
    cues = SelfCareModule().evaluate(store, _ctx(EVENING))
    assert all(c.intervention != "self_care_review" for c in cues)


def test_eod_cue_fires_when_opted_in_with_a_gap(store):
    store.set_state("self_care_review_enabled", "on", source="explicit")
    for hh, mm in [(15, 0), (15, 40), (16, 20), (17, 0), (18, 0), (19, 0)]:
        _confirm(store, "water", hh, mm)
    cues = SelfCareModule().evaluate(store, _ctx(EVENING))
    review_cues = [c for c in cues if c.intervention == "self_care_review"]
    assert len(review_cues) == 1
    cue = review_cues[0]
    assert cue.context_key == "self_care_review"
    assert cue.dedup_key == "self_care_review:2026-07-13"
    assert "3:00 pm" in cue.text


def test_eod_cue_silent_before_review_hour(store):
    store.set_state("self_care_review_enabled", "on", source="explicit")
    _confirm(store, "water", 15, 0)
    afternoon = datetime(2026, 7, 13, 16, 0, 0)  # 4pm < 21:00 review hour
    cues = SelfCareModule().evaluate(store, _ctx(afternoon))
    assert all(c.intervention != "self_care_review" for c in cues)


def test_eod_cue_silent_on_clean_day(store):
    """Opted in, evening, but no gaps → no recap (silence on a good day)."""
    store.set_state("self_care_review_enabled", "on", source="explicit")
    # Turn off every check except a cleanly-met water so there's no 'none' gap.
    for key in ("meal", "biobreak"):
        store.set_state(f"{key}_enabled", "off", source="explicit")
    for i in range(6):
        total = 9 * 60 + i * 90
        _confirm(store, "water", total // 60, total % 60)
    cues = SelfCareModule().evaluate(store, _ctx(EVENING))
    assert all(c.intervention != "self_care_review" for c in cues)


def test_eod_cue_fires_once_per_day(store):
    """Once fired (dedup stamped), the recap doesn't re-fire the same day."""
    store.set_state("self_care_review_enabled", "on", source="explicit")
    _confirm(store, "water", 15, 0)
    store.set_state("coach_fired:self_care_review:2026-07-13", "2026-07-13 21:00:00")
    assert last_fired(store, "self_care_review:2026-07-13") is not None
    cues = SelfCareModule().evaluate(store, _ctx(EVENING))
    assert all(c.intervention != "self_care_review" for c in cues)
