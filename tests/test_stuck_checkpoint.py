"""Stuck-checkpoint feature: snooze-aware avoidance, the long-avoided triage fork.

Covers the pure detection helpers in :mod:`prefrontal.todos`, the ``defer_todo``
repo method, the give-up-vs-hygiene fix for a consciously-parked drop, the
:mod:`prefrontal.modules.stuck_checkpoint` cue gating, and the briefing's calm
"🧭 Time to decide" surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prefrontal.briefing import build_briefing, render_briefing
from prefrontal.coaching import CoachContext, Decision
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.modules import get
from prefrontal.modules.stuck_checkpoint import _marker_key
from prefrontal.todos import (
    DEFAULT_CHECKPOINT_MIN_DAYS,
    DISCARDED_OUTCOME,
    avoidance_score,
    avoided_todos,
    deadline_pressure,
    is_snoozed,
    long_avoided_todos,
    todo_episode_fields,
)
from tests.conftest import scoped_default

TS = "%Y-%m-%d %H:%M:%S"


@pytest.fixture()
def store():
    """A MemoryStore on a fresh in-memory, schema-initialized DB (default user)."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _age(store, todo_id, days):
    """Backdate a todo's created_at so it's ``days`` old."""
    ts = (utcnow() - timedelta(days=days)).strftime(TS)
    store.conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (ts, todo_id))
    store.conn.commit()


def _aged(**kw):
    """A bare open-todo dict aged ~25 days (well past the checkpoint floor)."""
    base = {
        "id": 1,
        "title": "Renew passport",
        "status": "open",
        "priority": 2,
        "estimate_minutes": 20,
        "deadline": None,
        "created_at": "2026-06-01 12:00:00",
    }
    base.update(kw)
    return base


NOW = datetime(2026, 6, 26, 12, 0, 0)  # 25 days after the default created_at


# -- snooze awareness --------------------------------------------------------


def test_is_snoozed_future_past_and_absent():
    assert is_snoozed(_aged(snoozed_until="2026-06-27 12:00:00"), NOW) is True   # future
    assert is_snoozed(_aged(snoozed_until="2026-06-25 12:00:00"), NOW) is False  # lapsed
    assert is_snoozed(_aged(), NOW) is False                                     # unset
    assert is_snoozed(_aged(snoozed_until="not-a-date"), NOW) is False           # unparseable


def test_snooze_removes_an_item_from_avoidance():
    """A consciously-deferred todo scores 0 and drops out of the avoided set — it's a
    decision, not avoidance — until the defer lapses, then it re-enters untouched."""
    assert avoidance_score(_aged(), NOW) > 0
    assert avoidance_score(_aged(snoozed_until="2026-06-27 12:00:00"), NOW) == 0.0
    assert avoided_todos([_aged(snoozed_until="2026-06-27 12:00:00")], NOW) == []
    # A lapsed defer is just an active todo again.
    assert avoided_todos([_aged(snoozed_until="2026-06-25 12:00:00")], NOW) != []


# -- long_avoided_todos + deadline_pressure ----------------------------------


def test_long_avoided_todos_threshold():
    """Only items past the checkpoint floor count; it's a strict subset of avoided."""
    young = _aged(id=1, created_at="2026-06-16 12:00:00")   # 10d — avoided, not long
    old = _aged(id=2, created_at="2026-06-01 12:00:00")     # 25d — long-avoided
    todos = [young, old]
    assert {a["todo"]["id"] for a in avoided_todos(todos, NOW)} == {1, 2}
    assert [a["todo"]["id"] for a in long_avoided_todos(todos, NOW)] == [2]
    # Boundary: exactly at the floor qualifies.
    at_floor = _aged(
        id=3,
        created_at=(NOW - timedelta(days=DEFAULT_CHECKPOINT_MIN_DAYS)).strftime(TS),
    )
    assert [a["todo"]["id"] for a in long_avoided_todos([at_floor], NOW)] == [3]


def test_deadline_pressure_categories():
    assert deadline_pressure(_aged(deadline="2026-06-20"), NOW) == "overdue"
    assert deadline_pressure(_aged(deadline="2026-06-27"), NOW) == "imminent"
    assert deadline_pressure(_aged(deadline="2026-08-01"), NOW) is None
    assert deadline_pressure(_aged(deadline=None), NOW) is None


# -- give-up vs hygiene on drop ----------------------------------------------


def test_parked_then_dropped_is_hygiene_not_a_slip():
    """Dropping a consciously-parked (snoozed) todo is triage, not a give-up: it must
    not count as a `miss`, or the checkpoint would punish the behavior it enables."""
    now = utcnow()
    old = (now - timedelta(days=25)).strftime(TS)
    dropped = {"status": "dropped", "title": "Renew passport", "priority": 2, "created_at": old}
    # Aged + real priority alone → a give-up miss (baseline).
    assert todo_episode_fields(dropped, now=now)["outcome"] == "miss"
    # Same item, but it had been parked → hygiene discard.
    parked = {**dropped, "snoozed_until": (now - timedelta(days=1)).strftime(TS)}
    assert todo_episode_fields(parked, now=now)["outcome"] == DISCARDED_OUTCOME
    # Started still wins over parked — engaged-then-abandoned is the real miss.
    started = {**parked, "started_at": (now - timedelta(days=2)).strftime(TS)}
    assert todo_episode_fields(started, now=now)["outcome"] == "miss"


# -- defer_todo repo method --------------------------------------------------


def test_defer_todo_parks_and_clears(store):
    tid = store.add_todo("Renew passport", estimate_minutes=20, priority=2)
    _age(store, tid, 25)
    assert avoided_todos(store.open_todos(), utcnow())  # avoided before deferral

    until = (utcnow() + timedelta(days=7)).strftime(TS)
    assert store.defer_todo(tid, until) is True
    assert store.get_todo(tid)["snoozed_until"] == until
    assert avoided_todos(store.open_todos(), utcnow()) == []  # parked → not avoided

    assert store.defer_todo(tid, None) is True                # un-park
    assert store.get_todo(tid)["snoozed_until"] is None
    assert avoided_todos(store.open_todos(), utcnow())        # avoided again

    store.close_todo(tid, status="done")
    assert store.defer_todo(tid, until) is False              # closed todos aren't deferrable


# -- the module cue ----------------------------------------------------------


def _stuck_todo(store, *, days=25, overdue=True):
    """A long-avoided todo with (by default) an overdue deadline — the cue's trigger."""
    deadline = (utcnow() - timedelta(days=1 if overdue else -30)).strftime("%Y-%m-%d")
    tid = store.add_todo("Renew passport", estimate_minutes=20, priority=3, deadline=deadline)
    _age(store, tid, days)
    return tid


def test_checkpoint_fires_for_long_avoided_and_deadline_pressured(store):
    tid = _stuck_todo(store)
    cues = get("stuck_checkpoint").evaluate(store, CoachContext(now=utcnow()))
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "stuck_checkpoint"
    assert cue.intervention == "triage_fork"
    assert cue.ref["todo_id"] == tid and cue.ref["pressure"] == "overdue"
    assert cue.dedup_key == f"stuck_checkpoint:{tid}"
    # The three-way fork is present, framed as a decision (not a scolding).
    assert "break it into a first step" in cue.text
    assert "park it" in cue.text and "let it go" in cue.text


def test_checkpoint_silent_without_deadline_pressure(store):
    """Long-avoided but no looming deadline → the calm briefing handles it, no push."""
    _stuck_todo(store, overdue=False)
    assert get("stuck_checkpoint").evaluate(store, CoachContext(now=utcnow())) == []


def test_checkpoint_silent_under_the_floor(store):
    """Avoided but not for weeks → not a checkpoint yet, even if deadline-pressured."""
    _stuck_todo(store, days=10)
    assert get("stuck_checkpoint").evaluate(store, CoachContext(now=utcnow())) == []


def test_checkpoint_yields_to_a_claimed_todo(store):
    """If a higher-value cue already claimed the todo this tick, the checkpoint stands
    down rather than double-nudging the same item."""
    tid = _stuck_todo(store)
    ctx = CoachContext(now=utcnow())
    ctx.claimed_todo_ids.add(tid)
    assert get("stuck_checkpoint").evaluate(store, ctx) == []


def test_checkpoint_fires_once_per_item(store):
    """after_fire stamps a per-todo marker; the item is never re-checkpointed."""
    tid = _stuck_todo(store)
    module = get("stuck_checkpoint")
    ctx = CoachContext(now=utcnow())
    cues = module.evaluate(store, ctx)
    assert len(cues) == 1

    module.after_fire(store, [Decision(cue=cues[0], channel="push", text=cues[0].text)], ctx)
    assert store.get_state(_marker_key(tid)) is not None
    # Still long-avoided and overdue, but already asked once → silent.
    assert module.evaluate(store, CoachContext(now=utcnow())) == []


# -- the briefing surface ----------------------------------------------------


def test_briefing_partitions_sliding_from_time_to_decide(store):
    """A weeks-avoided item lands in 🧭 Time to decide; a normal slide stays in 🐢
    Keeps sliding — each item shows in exactly one block."""
    slide = store.add_todo("Book dentist", estimate_minutes=15, priority=2)
    _age(store, slide, 10)                                   # 10d → normal slide
    stuck = store.add_todo("Renew passport", estimate_minutes=20, priority=3)
    _age(store, stuck, 25)                                   # 25d → checkpoint

    briefing = build_briefing(store, now=utcnow())
    slide_ids = {a["todo_id"] for a in briefing.avoided}
    stuck_ids = {a["todo_id"] for a in briefing.checkpoint}
    assert slide in slide_ids and slide not in stuck_ids
    assert stuck in stuck_ids and stuck not in slide_ids

    text = render_briefing(briefing)
    assert "🧭 Time to decide" in text
    assert "Renew passport" in text and "break it down, defer, or drop it" in text
