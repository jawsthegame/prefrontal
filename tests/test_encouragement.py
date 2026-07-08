"""Tests for the encouragement & recovery layer (prefrontal/encouragement.py).

Model-free: the day assessment / scoring, the recovery plan, tone rendering, the
once-per-day debounce cursor, the Ollama-with-fallback pass, and the
GET /encouragement + POST /encouragement/sent surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.commitments import conflict_dismissal_key, find_conflicts
from prefrontal.config import Settings
from prefrontal.encouragement import (
    already_sent_today,
    assess_day,
    build_recovery,
    mark_sent_today,
    render_encouragement,
    summarize_encouragement,
)
from prefrontal.impact import utcnow
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.db import init_db
from prefrontal.memory.repos.episodes import EpisodesRepo
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "enc-secret"

#: A fixed reference instant for the whole suite — a midday, mid-month time far
#: from any date boundary. The assessment scopes signals to "today"
#: (``day_start = midnight``), so these tests were flaky whenever their separate
#: clock reads — ``utcnow()`` here, SQLite ``CURRENT_TIMESTAMP`` inside
#: ``log_episode``, and a second ``utcnow()`` *inside* the HTTP handler — straddled
#: a UTC midnight: the miss episode then fell outside "today" and the day read
#: not-rough. Freezing every one of those reads to :data:`_NOW` removes the race.
_NOW = datetime(2026, 6, 15, 12, 0, 0)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Freeze every clock these tests depend on to :data:`_NOW` (deterministic).

    Pins the three ``utcnow()`` read sites (this module, the encouragement core,
    and the coaching router used by the endpoint) *and* the episode timestamp,
    which is otherwise ``CURRENT_TIMESTAMP`` (the real clock). With all four on the
    same instant, "today"-scoped assessment is stable regardless of wall time.
    """
    ts = _NOW.strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr("tests.test_encouragement.utcnow", lambda: _NOW)
    monkeypatch.setattr("prefrontal.encouragement.utcnow", lambda: _NOW)
    monkeypatch.setattr("prefrontal.webhooks.routers.coaching.utcnow", lambda: _NOW)
    _orig_log = EpisodesRepo.log_episode

    def _log_pinned(self, *args, **kwargs):
        kwargs.setdefault("timestamp", ts)  # default to the frozen clock, not CURRENT_TIMESTAMP
        return _orig_log(self, *args, **kwargs)

    monkeypatch.setattr(EpisodesRepo, "log_episode", _log_pinned)


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        s = scoped_default(MemoryStore(conn))
        s.set_state("encouragement", "on", source="explicit")  # opt in for most tests
        yield s
    finally:
        conn.close()


def _at(now, **kw) -> str:
    return now.replace(**kw).strftime("%Y-%m-%d %H:%M:%S")


def _hard_commitment(store, now, title="Dentist"):
    store.upsert_commitment(
        title=title, start_at=_at(now, hour=9, minute=0, second=0, microsecond=0),
        lead_minutes=10.0, hardness="hard", source="manual",
    )


# -- assessment / scoring ----------------------------------------------------


def test_off_by_default_is_never_rough():
    conn = init_db(":memory:")
    try:
        s = scoped_default(MemoryStore(conn))  # encouragement key unset → off
        s.log_episode("departure", outcome="miss", context="commitment: Dentist")
        a = assess_day(s, now=utcnow())
        assert a.enabled is False and a.rough is False and a.rough_score == 0.0
    finally:
        conn.close()


def test_one_missed_hard_commitment_trips_the_threshold(store):
    now = utcnow()
    _hard_commitment(store, now)
    store.log_episode("departure", outcome="miss", context="commitment: Dentist")
    a = assess_day(store, now=now)
    assert a.rough is True and a.rough_score == 3.0
    assert [s["kind"] for s in a.signals] == ["missed_hard"]


def test_one_stray_miss_is_not_rough(store):
    a = assess_day(store, now=utcnow())
    assert a.rough is False
    store.log_episode("task", outcome="miss", context="todo dropped: laundry")
    a = assess_day(store, now=utcnow())
    assert a.rough_score == 1.0 and a.rough is False  # 1.0 < 3.0


def test_three_misses_trip_the_threshold(store):
    for i in range(3):
        store.log_episode("reminder", outcome="miss", context=f"ignored {i}")
    a = assess_day(store, now=utcnow())
    assert a.rough_score == 3.0 and a.rough is True
    assert all(s["kind"] == "miss_episode" for s in a.signals)


def test_overwhelmed_plate_trips_via_panic_signal(store):
    # No misses logged, but two hard commitments are already underway → panic
    # mode reads the plate as "overwhelmed" (>=2 already late), which is itself a
    # rough-day trigger (reusing overwhelm_level, weighted like a missed-hard).
    now = utcnow()
    for hour, title in ((9, "Standup"), (11, "Review")):
        store.upsert_commitment(
            title=title,
            start_at=_at(now, hour=hour, minute=0, second=0, microsecond=0),
            lead_minutes=10.0, hardness="hard", source="manual",
        )
    a = assess_day(store, now=now)
    assert a.rough is True and a.rough_score == 3.0
    assert [s["kind"] for s in a.signals] == ["overwhelmed"]


def test_single_late_commitment_is_not_overwhelmed(store):
    # One hard commitment underway is normal life, not overwhelm — no signal, and
    # the day stays calm (guards the overwhelm trigger's bar against firing early).
    now = utcnow()
    _hard_commitment(store, now)  # 9am hard, started by the frozen noon
    a = assess_day(store, now=now)
    assert a.rough is False
    assert not any(s["kind"] == "overwhelmed" for s in a.signals)


def test_a_double_booking_contributes_a_conflict_signal(store):
    # Two real, overlapping commitments today (neither dismissed, neither hard) are
    # a structural double-booking — the one signal weighted 0.5. It shows up in the
    # assessment but a lone conflict is well under the 3.0 rough threshold.
    now = _NOW
    for title in ("Call Alice", "Call Bob"):
        store.upsert_commitment(
            title=title,
            start_at=_at(now, hour=15, minute=0, second=0, microsecond=0),
            source="manual",
        )
    a = assess_day(store, now=now)
    assert [s["kind"] for s in a.signals] == ["conflict"]
    assert a.rough_score == 0.5 and a.rough is False  # 0.5 < 3.0


def test_dismissed_double_booking_contributes_no_conflict_signal(store):
    # Dismissing the double-booking ("it's fine") removes its stress weight.
    now = _NOW
    for title in ("Call Alice", "Call Bob"):
        store.upsert_commitment(
            title=title,
            start_at=_at(now, hour=15, minute=0, second=0, microsecond=0),
            source="manual",
        )
    # Build the conflict from the day's commitments the same way assess_day does
    # (upcoming_commitments would filter by the real clock, past the frozen _NOW).
    today = store.commitments_between("2026-06-15 00:00:00", "2026-06-16 00:00:00")
    conflicts = find_conflicts(today)
    store.dismiss_conflict(conflict_dismissal_key(conflicts[0]))
    a = assess_day(store, now=now)
    assert not any(s["kind"] == "conflict" for s in a.signals)
    assert a.rough_score == 0.0


def test_drift_modifier_tips_a_borderline_day(store):
    # Two misses (2.0) is under threshold; a rising-drift modifier (+0.5) still
    # isn't enough on its own — but lowering the threshold shows it's applied.
    store.log_episode("task", outcome="miss")
    store.log_episode("task", outcome="miss")
    store.upsert_pattern("drift", "task", observed_value=0.8, sample_size=10, confidence=0.9)
    a = assess_day(store, now=utcnow())
    assert a.rough_score == 2.5  # 2×1.0 + 0.5 drift modifier


# -- recovery plan -----------------------------------------------------------


def test_recovery_empty_when_not_rough(store):
    plan = build_recovery(store, assess_day(store, now=utcnow()), now=utcnow())
    assert plan.refit == [] and plan.defer == [] and plan.first_step is None


def test_recovery_first_step_from_most_avoided_todo(store):
    now = utcnow()
    _hard_commitment(store, now)
    store.log_episode("departure", outcome="miss", context="commitment: Dentist")
    tid = store.add_todo("Call the accountant", priority=2)
    store.conn.execute(
        "UPDATE todos SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), tid),
    )
    store.conn.commit()
    plan = build_recovery(store, assess_day(store, now=now), now=now)
    assert plan.first_step is not None
    assert plan.first_step["todo_id"] == tid
    assert plan.first_step["minutes"] <= 5.0  # the decomposition ceiling


def test_recovery_defers_only_soft_commitments(store):
    # Pin to a fixed midday: after the 09:00 hard commitment (so it reads as a
    # missed-hard signal → the day is "rough") but before the 15:00 soft one (the
    # defer filter only suggests commitments with start_at >= now). With a bare
    # utcnow() the test flakes once the wall clock passes 15:00 UTC.
    now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    _hard_commitment(store, now, title="Dentist")  # hard → never deferred
    store.upsert_commitment(
        title="Optional coffee", start_at=_at(now, hour=15, minute=0, second=0, microsecond=0),
        hardness="soft", source="manual",
    )
    store.log_episode("departure", outcome="miss", context="commitment: Dentist")
    plan = build_recovery(store, assess_day(store, now=now), now=now)
    titles = [d["title"] for d in plan.defer]
    assert "Optional coffee" in titles and "Dentist" not in titles


# -- rendering / tone --------------------------------------------------------


def test_render_empty_when_not_rough(store):
    assert render_encouragement(assess_day(store, now=utcnow()), build_recovery(
        store, assess_day(store, now=utcnow()), now=utcnow())) == ""


def test_render_tone_warm_vs_plain(store):
    now = utcnow()
    for _ in range(3):
        store.log_episode("task", outcome="miss")
    warm = render_encouragement(assess_day(store, now=now), build_recovery(
        store, assess_day(store, now=now), now=now))
    assert "referendum" in warm  # the warm acknowledgement

    store.set_state("encouragement_tone", "plain", source="explicit")
    plain = render_encouragement(assess_day(store, now=now), build_recovery(
        store, assess_day(store, now=now), now=now))
    assert "ran rough" in plain and "referendum" not in plain


def test_render_uses_local_time_for_refit_windows(store):
    """Rough-day refit window times read in the local zone, not raw UTC.

    The re-fit windows are naive UTC; _NOW (12:00 UTC) is 08:00 EDT. The digest
    must show "8:00", not "12:00" (the old bug sliced the UTC string).
    """
    now = _NOW
    for _ in range(3):
        store.log_episode("task", outcome="miss")  # make the day rough
    store.add_todo("Sort the mail", estimate_minutes=30, priority=1)
    assessment = assess_day(store, now=now)
    plan = build_recovery(store, assessment, now=now)
    text = render_encouragement(assessment, plan, tz="America/New_York")
    assert "What still fits today" in text and plan.refit  # a window was fit
    assert "8:00" in text  # 12:00 UTC window start, shown in EDT
    assert "12:00" not in text  # the UTC wall clock never leaks through


# -- debounce cursor ---------------------------------------------------------


def test_debounce_cursor_is_per_day(store):
    now = utcnow()
    assert already_sent_today(store, now=now) is False
    mark_sent_today(store, now=now)
    assert already_sent_today(store, now=now) is True
    # A new UTC day resets it.
    assert already_sent_today(store, now=now + timedelta(days=1)) is False


# -- optional prose pass -----------------------------------------------------


class _FakeClient:
    def __init__(self, reply="", error=False, model="fake"):
        self.reply, self.error, self.model = reply, error, model

    def generate(self, prompt, *, system=None):
        if self.error:
            raise OllamaError("down")
        return self.reply


def test_summarize_falls_back_on_model_failure(store):
    for _ in range(3):
        store.log_episode("task", outcome="miss")
    ok = summarize_encouragement(store, client=_FakeClient(reply="Hey, rough one. Start here."))
    assert ok.source == "llm" and ok.rough is True and "rough" in ok.text.lower()

    fb = summarize_encouragement(store, client=_FakeClient(error=True))
    assert fb.source == "heuristic" and fb.rough is True


def test_summarize_not_rough_returns_empty(store):
    res = summarize_encouragement(store, client=_FakeClient(reply="unused"))
    assert res.rough is False and res.text == ""


# -- HTTP surface ------------------------------------------------------------


def _http_store():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
    scoped.set_state("encouragement", "on", source="explicit")
    return conn, unscoped, scoped


def test_encouragement_endpoint_and_debounce():
    conn, unscoped, scoped = _http_store()
    try:
        now = utcnow()
        scoped.upsert_commitment(
            title="Dentist", start_at=now.replace(hour=9, minute=0, second=0, microsecond=0)
            .strftime("%Y-%m-%d %H:%M:%S"), lead_minutes=10.0, hardness="hard", source="manual",
        )
        scoped.log_episode("departure", outcome="miss", context="commitment: Dentist")
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": SECRET}
            assert c.get("/encouragement").status_code == 401  # auth required
            body = c.get("/encouragement", headers=hdr).json()
            assert body["rough"] is True and body["already_sent"] is False
            assert body["text"] and any(s["kind"] == "missed_hard" for s in body["signals"])
            # Stamp delivered → a later read reports already_sent.
            c.post("/encouragement/sent", headers=hdr)
            assert c.get("/encouragement", headers=hdr).json()["already_sent"] is True
    finally:
        conn.close()


def test_encouragement_endpoint_calm_day():
    conn, unscoped, scoped = _http_store()
    try:
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            body = c.get("/encouragement", headers={"X-Prefrontal-Token": SECRET}).json()
            assert body["rough"] is False and body["text"] == ""
    finally:
        conn.close()
