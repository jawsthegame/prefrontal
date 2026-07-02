"""Tests for the coaching-agent decision engine (prefrontal/coaching.py).

Model-free: the pure core (cue collection, channel choice, suppression) plus the
Task Paralysis evaluator that fires the first real coaching cue.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from prefrontal.coaching import (
    CoachContext,
    Cue,
    choose_channel,
    collect_cues,
    decide,
    record_fired,
    suppressed,
)
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.modules import get
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "coach-secret"

NOON = datetime(2026, 7, 2, 12, 0, 0)  # inside the default 8–22 responsive window


class _FakeStore:
    """Minimal store stub for the pure channel/suppression tests."""

    def __init__(self, *, patterns=None, state=None, floats=None):
        self._patterns = patterns or []
        self._state = dict(state or {})
        self._floats = floats or {}

    def get_patterns(self, pattern_type=None):
        return [
            p for p in self._patterns
            if pattern_type is None or p["pattern_type"] == pattern_type
        ]

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value, source="inferred"):
        self._state[key] = value

    def get_float(self, key, default):
        return self._floats.get(key, default)


def _ctx(**kw) -> CoachContext:
    return CoachContext(now=kw.pop("now", NOON), **kw)


def _cue(urgency="nudge", dedup="k", **kw) -> Cue:
    return Cue(
        module="m", intervention="i", urgency=urgency, text="hi",
        context_key="todo", dedup_key=dedup, **kw,
    )


# -- channel choice -----------------------------------------------------------


def test_channel_starts_at_urgency_floor():
    store = _FakeStore()
    assert choose_channel(store, _cue("ambient"), _ctx()) == "digest"
    assert choose_channel(store, _cue("nudge"), _ctx()) == "push"
    assert choose_channel(store, _cue("critical"), _ctx()) == "voice"


def test_channel_bumps_when_floor_channel_is_ignored():
    """A reliably-ignored push (low ack, enough samples) bumps a nudge to sound."""
    store = _FakeStore(patterns=[
        {"pattern_type": "channel_response", "context_key": "push",
         "observed_value": 0.1, "sample_size": 10},
    ])
    assert choose_channel(store, _cue("nudge"), _ctx()) == "sound"


def test_channel_does_not_bump_without_enough_samples():
    store = _FakeStore(patterns=[
        {"pattern_type": "channel_response", "context_key": "push",
         "observed_value": 0.0, "sample_size": 1},  # too few to trust
    ])
    assert choose_channel(store, _cue("nudge"), _ctx()) == "push"


def test_suggested_channel_can_raise_but_critical_is_always_voice():
    store = _FakeStore()
    assert choose_channel(store, _cue("nudge", suggested_channel="sound"), _ctx()) == "sound"
    # A module hint can't lower a critical off voice.
    assert choose_channel(store, _cue("critical", suggested_channel="push"), _ctx()) == "voice"


# -- suppression: quiet hours + debounce -------------------------------------


def test_quiet_hours_hold_everything_but_critical():
    store = _FakeStore()
    night = _ctx(now=datetime(2026, 7, 2, 3, 0, 0))  # 3am, outside 8–22
    assert suppressed(store, _cue("nudge"), night) is True
    assert suppressed(store, _cue("urgent"), night) is True
    assert suppressed(store, _cue("critical"), night) is False


def test_within_hours_not_suppressed():
    assert suppressed(_FakeStore(), _cue("nudge"), _ctx()) is False


def test_debounce_holds_a_recently_fired_cue():
    fired = (NOON - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    store = _FakeStore(state={"coach_fired:k": fired})
    assert suppressed(store, _cue(dedup="k"), _ctx()) is True  # within 120m default
    # Past the window it's free again.
    old = (NOON - timedelta(minutes=200)).strftime("%Y-%m-%d %H:%M:%S")
    store2 = _FakeStore(state={"coach_fired:k": old})
    assert suppressed(store2, _cue(dedup="k"), _ctx()) is False


def test_decide_drops_suppressed_and_record_fired_stamps():
    fired = (NOON - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    store = _FakeStore(state={"coach_fired:recent": fired})
    cues = [_cue(dedup="recent"), _cue(dedup="fresh")]
    decisions = decide(store, cues, _ctx())
    assert [d.cue.dedup_key for d in decisions] == ["fresh"]
    record_fired(store, decisions, NOON)
    assert store.get_state("coach_fired:fresh") is not None


def test_collect_cues_isolates_a_broken_module():
    class Boom:
        key = "boom"

        def evaluate(self, store, ctx):
            raise RuntimeError("evaluator blew up")

    class Ok:
        key = "ok"

        def evaluate(self, store, ctx):
            return [_cue()]

    cues = collect_cues(_FakeStore(), [Boom(), Ok()], _ctx())
    assert len(cues) == 1  # the good module still contributes


# -- Task Paralysis evaluator (the first real cue source) --------------------


def _store_with_avoided_todo():
    store = scoped_default(MemoryStore(init_db(":memory:")))
    tid = store.add_todo("Call the accountant", priority=2)
    old = (NOON - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (old, tid))
    store.conn.commit()
    return store, tid


def test_task_paralysis_evaluator_fires_tiny_first_step():
    store, tid = _store_with_avoided_todo()
    cues = get("task_paralysis").evaluate(store, _ctx())
    assert len(cues) == 1
    cue = cues[0]
    assert cue.intervention == "tiny_first_step"
    assert cue.urgency == "nudge"
    assert cue.ref == {"todo_id": tid}
    assert "Call the accountant" in cue.text and "Start tiny" in cue.text
    assert cue.dedup_key == f"tiny_first_step:{tid}"


def test_task_paralysis_evaluator_silent_when_nothing_avoided():
    with MemoryStore.open(":memory:") as root:
        store = scoped_default(root)
        store.add_todo("fresh task")  # created now → not yet avoided
        assert get("task_paralysis").evaluate(store, _ctx()) == []


def test_task_paralysis_evaluator_prefers_the_decomposition_first_step():
    store, tid = _store_with_avoided_todo()
    store.set_decomposition(
        tid,
        first_step="Find the accountant's number and dial.",
        first_step_minutes=2.0,
        steps=[],
        source="heuristic",
    )
    cue = get("task_paralysis").evaluate(store, _ctx())[0]
    assert "Find the accountant's number and dial." in cue.text


# -- POST /webhooks/coach/check ----------------------------------------------


def _http_store_with_avoided_todo():
    """An unscoped store (for create_app) whose one user has an avoided todo."""
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
    # Always-responsive window so the tick isn't flakily suppressed by wall-clock.
    scoped.set_state("responsive_hours_start", "0", source="explicit")
    scoped.set_state("responsive_hours_end", "0", source="explicit")
    tid = scoped.add_todo("Call the accountant", priority=2)
    old = (utcnow() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    scoped.conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (old, tid))
    scoped.conn.commit()
    return conn, unscoped


def test_coach_check_requires_auth():
    conn = init_db(":memory:")
    try:
        app = create_app(store=MemoryStore(conn), settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            assert c.post("/webhooks/coach/check", json={}).status_code == 401
    finally:
        conn.close()


def test_coach_check_fires_a_cue_then_debounces():
    conn, unscoped = _http_store_with_avoided_todo()
    try:
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": SECRET}
            first = c.post("/webhooks/coach/check", json={}, headers=hdr).json()["cues"]
            assert len(first) == 1
            cue = first[0]
            assert cue["module"] == "task_paralysis"
            assert cue["intervention"] == "tiny_first_step"
            assert cue["channel"] == "push" and cue["fire"] is True
            assert "Call the accountant" in cue["text"]
            # Recorded as fired → the same cue is debounced on the next poll.
            second = c.post("/webhooks/coach/check", json={}, headers=hdr).json()["cues"]
            assert second == []
    finally:
        conn.close()
