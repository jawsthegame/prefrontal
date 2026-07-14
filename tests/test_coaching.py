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
    Decision,
    build_context,
    choose_channel,
    collect_cues,
    decide,
    dosage_count_today,
    in_quiet_hours,
    note_delivered,
    phrase,
    receptive,
    record_fired,
    resolve_ack,
    stamp_pending_engagement,
    suppressed,
    sweep_engagement,
    sweep_stale_nudges,
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

# The combined channel-ack target map the engine builds from the enabled modules
# (see coaching.channel_targets_for); passed to note_delivered in these unit
# tests, which exercise it directly rather than through run_coaching_tick.
_TRACKED = {"outing": "outing_id", "departure": "commitment_id", "focus": "session_id"}


class _FakeStore:
    """Minimal store stub for the pure channel/suppression tests."""

    def __init__(self, *, patterns=None, state=None, floats=None):
        self._patterns = patterns or []
        self._state = dict(state or {})
        self._floats = floats or {}
        self.episodes = []

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

    def delete_state(self, key):
        self._state.pop(key, None)

    def all_state(self):
        return {k: {"value": v} for k, v in self._state.items()}

    def log_episode(self, episode_type, **kw):
        self.episodes.append({"episode_type": episode_type, **kw})
        return len(self.episodes)

    def episodes_by_type(self, episode_type, limit=100, *, context_prefix=None):
        # Newest-first, like the real store (which orders by timestamp/id desc),
        # with the same SQL-level context-prefix filter applied before the limit.
        matching = [
            e
            for e in self.episodes
            if e.get("episode_type") == episode_type
            and (
                context_prefix is None
                or str(e.get("context") or "").startswith(context_prefix)
            )
        ]
        return list(reversed(matching))[:limit]


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


# -- receptivity gate (M3): default to silence when not reachable -------------


def _nudge_outcome(acknowledged: bool, context: str = "coach nudge: todo") -> dict:
    """A `coach nudge` channel-outcome episode as record_channel_outcome writes it."""
    return {
        "episode_type": "reminder",
        "context": context,
        "outcome": "success" if acknowledged else "miss",
        "acknowledged": acknowledged,
    }


def test_receptive_true_without_enough_ignore_history():
    # No coaching outcomes at all → receptive (not enough evidence to go quiet).
    assert receptive(_FakeStore(), _ctx()) is True
    # Two ignores is below the default streak of 3 → still receptive.
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False), _nudge_outcome(False)]
    assert receptive(store, _ctx()) is True


def test_receptive_false_after_consecutive_ignores():
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False) for _ in range(3)]
    assert receptive(store, _ctx()) is False


def test_receptive_reset_by_a_single_recent_ack():
    # Most-recent (last-appended) is an ack, so the latest run isn't all-misses.
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False), _nudge_outcome(False), _nudge_outcome(True)]
    assert receptive(store, _ctx()) is True


def test_receptive_ignores_non_coach_reminder_episodes():
    # A run of ignored *non-coaching* reminders must not trigger the backoff.
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False, context="departure reminder") for _ in range(5)]
    assert receptive(store, _ctx()) is True


def test_receptive_backoff_not_masked_by_interleaved_non_coach_reminders():
    # Three ignored coach nudges, then a burst of unrelated reminders. A fixed
    # scan-then-filter window would let the burst crowd the coach misses out of
    # view (falsely receptive); the SQL context-prefix filter still sees them.
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False) for _ in range(3)]
    store.episodes += [_nudge_outcome(False, context="ingest reminder") for _ in range(20)]
    assert receptive(store, _ctx()) is False


def test_receptive_disabled_by_zero_threshold():
    store = _FakeStore(floats={"coach_ignore_backoff_streak": 0})
    store.episodes = [_nudge_outcome(False) for _ in range(5)]
    assert receptive(store, _ctx()) is True


def test_receptive_fails_open_when_read_raises():
    class _Broken(_FakeStore):
        def episodes_by_type(self, episode_type, limit=100):
            raise RuntimeError("boom")

    assert receptive(_Broken(), _ctx()) is True


def test_decide_holds_non_critical_when_not_receptive_but_lets_critical_through():
    store = _FakeStore()
    store.episodes = [_nudge_outcome(False) for _ in range(3)]  # backing off
    cues = [_cue("nudge", dedup="a"), _cue("urgent", dedup="b"), _cue("critical", dedup="c")]
    decisions = decide(store, cues, _ctx())
    # Only the critical cue survives the receptivity gate.
    assert [d.cue.dedup_key for d in decisions] == ["c"]


def test_decide_fires_normally_when_receptive():
    store = _FakeStore()  # no ignore history
    cues = [_cue("nudge", dedup="a"), _cue("nudge", dedup="b")]
    decisions = decide(store, cues, _ctx())
    assert [d.cue.dedup_key for d in decisions] == ["a", "b"]


# -- dosage cap (M3): the frequency half of the habituation guardrail ---------


def _capday(now, count):
    """A `coach_nudge_day` tally value for `now`'s day."""
    return f"{now.strftime('%Y-%m-%d')}|{count}"


def test_dosage_cap_holds_overflow_when_budget_exhausted():
    # Cap 3, already 2 fired today → budget 1; three fresh nudges due, one fires.
    store = _FakeStore(
        floats={"coach_daily_nudge_cap": 3},
        state={"coach_nudge_day": _capday(NOON, 2)},
    )
    cues = [_cue("nudge", dedup=f"k{i}") for i in range(3)]
    decisions = decide(store, cues, _ctx())
    assert len(decisions) == 1


def test_dosage_cap_highest_urgency_wins_the_budget():
    store = _FakeStore(
        floats={"coach_daily_nudge_cap": 1},  # room for exactly one
        state={"coach_nudge_day": _capday(NOON, 0)},
    )
    cues = [_cue("nudge", dedup="low"), _cue("urgent", dedup="high")]
    decisions = decide(store, cues, _ctx())
    assert [d.cue.dedup_key for d in decisions] == ["high"]


def test_dosage_cap_never_holds_critical_or_digest():
    store = _FakeStore(
        floats={"coach_daily_nudge_cap": 1},
        state={"coach_nudge_day": _capday(NOON, 5)},  # already way over
    )
    cues = [_cue("nudge", dedup="n"), _cue("critical", dedup="c"), _cue("ambient", dedup="a")]
    decisions = decide(store, cues, _ctx())
    keys = {d.cue.dedup_key for d in decisions}
    # The nudge is over-cap and held; critical and ambient (digest) always go.
    assert keys == {"c", "a"}


def test_dosage_cap_zero_disables():
    store = _FakeStore(
        floats={"coach_daily_nudge_cap": 0},
        state={"coach_nudge_day": _capday(NOON, 99)},
    )
    cues = [_cue("nudge", dedup=f"k{i}") for i in range(4)]
    assert len(decide(store, cues, _ctx())) == 4


def test_dosage_cap_tally_resets_on_a_new_day():
    # A tally from yesterday must not count against today's budget.
    yesterday = NOON - timedelta(days=1)
    store = _FakeStore(
        floats={"coach_daily_nudge_cap": 2},
        state={"coach_nudge_day": _capday(yesterday, 2)},
    )
    assert dosage_count_today(store, NOON) == 0
    cues = [_cue("nudge", dedup=f"k{i}") for i in range(2)]
    assert len(decide(store, cues, _ctx())) == 2


def test_record_fired_advances_dosage_tally_only_for_interrupting_noncritical():
    store = _FakeStore(floats={"coach_daily_nudge_cap": 10})
    decisions = [
        Decision(cue=_cue("nudge", dedup="n"), channel="push", text="x"),
        Decision(cue=_cue("critical", dedup="c"), channel="voice", text="x"),
        Decision(cue=_cue("ambient", dedup="a"), channel="digest", text="x"),
    ]
    record_fired(store, decisions, NOON)
    # Only the push nudge counts; critical and digest don't.
    assert dosage_count_today(store, NOON) == 1


def test_dosage_cap_accumulates_across_ticks():
    store = _FakeStore(floats={"coach_daily_nudge_cap": 2})
    # Tick 1: one nudge fires and is recorded.
    d1 = decide(store, [_cue("nudge", dedup="a")], _ctx())
    record_fired(store, d1, NOON)
    # Tick 2 (a bit later, past debounce is irrelevant — different keys): two due,
    # only one budget left.
    later = _ctx(now=NOON + timedelta(minutes=1))
    d2 = decide(store, [_cue("nudge", dedup="b"), _cue("nudge", dedup="c")], later)
    assert len(d2) == 1


# -- outcome loop: channel_response learning (spec §8) ------------------------


def _outing_cue(outing_id=7):
    return Cue(
        module="location_anchor", intervention="outing_firm", urgency="urgent",
        text="still out?", context_key="outing",
        dedup_key=f"outing_escalation:{outing_id}:firm", ref={"outing_id": outing_id},
    )


def test_note_delivered_tracks_only_button_bearing_interrupting_nudges():
    store = _FakeStore()
    decisions = [
        Decision(cue=_outing_cue(7), channel="push", text="x"),          # tracked
        Decision(cue=_cue(dedup="t"), channel="push", text="y"),         # todo: no buttons
        Decision(cue=_outing_cue(9), channel="digest", text="z"),        # digest: no interrupt
    ]
    note_delivered(store, decisions, NOON, targets=_TRACKED)
    keys = sorted(k for k in store._state if k.startswith("coach_ack:"))
    assert keys == ["coach_ack:outing:7"]  # only the interactive, non-digest cue
    assert store._state["coach_ack:outing:7"].split("|")[1] == "push"


def test_resolve_ack_logs_acknowledged_and_clears_marker():
    store = _FakeStore()
    note_delivered(
        store, [Decision(cue=_outing_cue(7), channel="sound", text="x")], NOON, targets=_TRACKED
    )
    assert resolve_ack(store, "outing", 7) is True
    assert store.episodes == [{
        "episode_type": "reminder", "channel": "sound", "acknowledged": True,
        "context": "coach nudge: outing", "outcome": "success",
    }]
    assert "coach_ack:outing:7" not in store._state       # marker cleared
    assert resolve_ack(store, "outing", 7) is False        # nothing left → no-op


def test_sweep_logs_unacked_as_miss_after_window_but_leaves_fresh_ones():
    store = _FakeStore()
    note_delivered(
        store, [Decision(cue=_outing_cue(1), channel="push", text="x")], NOON, targets=_TRACKED
    )
    note_delivered(
        store, [Decision(cue=_outing_cue(2), channel="push", text="y")],
        NOON - timedelta(minutes=90),  # delivered long ago → past the ack window
        targets=_TRACKED,
    )
    swept = sweep_stale_nudges(store, NOON, ack_window_minutes=60.0)
    assert swept == 1  # only the stale one
    assert store.episodes == [{
        "episode_type": "reminder", "channel": "push", "acknowledged": False,
        "context": "coach nudge: outing", "outcome": "miss",
    }]
    assert "coach_ack:outing:2" not in store._state   # swept marker cleared
    assert "coach_ack:outing:1" in store._state        # fresh one still pending


# -- engagement outcomes (stamp_pending_engagement / sweep_engagement) --------


def _project_cue(pid):
    return Cue(
        module="projects", intervention="staleness", urgency="nudge", text="x",
        context_key="project", dedup_key=f"project_stale:{pid}", ref={"project_id": pid},
    )


def test_stamp_pending_engagement_marks_once_per_target():
    store = _FakeStore()
    d = Decision(cue=_project_cue(7), channel="push", text="x")
    stamp_pending_engagement(store, [d], NOON, module="projects", target_key="project_id")
    stamp = NOON.strftime("%Y-%m-%d %H:%M:%S")
    assert store._state["coach_engage:projects:7"] == stamp
    # A re-nudge on the same target doesn't reset the window (first fire owns it).
    stamp_pending_engagement(
        store, [d], NOON + timedelta(days=1), module="projects", target_key="project_id"
    )
    assert store._state["coach_engage:projects:7"] == stamp
    # A cue from another module is left alone.
    other = Decision(cue=_cue("nudge"), channel="push", text="x")  # module="m"
    stamp_pending_engagement(store, [other], NOON, module="projects", target_key="project_id")
    assert list(store._state) == ["coach_engage:projects:7"]


def test_sweep_engagement_logs_decided_and_leaves_pending():
    store = _FakeStore(state={
        "coach_engage:projects:1": "2026-07-01 09:00:00",  # engaged → success
        "coach_engage:projects:2": "2026-07-01 09:00:00",  # verdict None → left
        "coach_engage:projects:3": "not-a-timestamp",       # malformed → dropped
    })

    def verdict(target, fired):
        return {"1": "engaged", "2": None}.get(target, "ignored")

    swept = sweep_engagement(
        store, NOON, module="projects", episode_type="project", verdict=verdict
    )
    assert swept == 1
    assert store.episodes == [{
        "episode_type": "project", "acknowledged": True,
        "context": "projects nudge: 1", "outcome": "success",
    }]
    assert "coach_engage:projects:1" not in store._state   # decided → cleared
    assert "coach_engage:projects:2" in store._state        # pending → left
    assert "coach_engage:projects:3" not in store._state    # malformed → dropped


def test_sweep_engagement_logs_ignored_and_scopes_to_module():
    store = _FakeStore(state={"coach_engage:projects:5": "2026-07-01 09:00:00"})
    # A different module's sweep never touches this marker.
    assert sweep_engagement(
        store, NOON, module="delegation_checkin", episode_type="delegation",
        verdict=lambda t, f: "ignored",
    ) == 0
    assert store.episodes == [] and "coach_engage:projects:5" in store._state
    # Its own module's sweep logs the ignored outcome and clears it.
    sweep_engagement(
        store, NOON, module="projects", episode_type="project", verdict=lambda t, f: "ignored"
    )
    assert store.episodes == [{
        "episode_type": "project", "acknowledged": False,
        "context": "projects nudge: 5", "outcome": "ignored",
    }]
    assert store._state == {}


def test_loop_closes_ignored_channel_bumps_after_learn():
    """End-to-end: ignored push nudges → learn → choose_channel bumps push→sound.

    The whole point of §8: deliveries that go unanswered teach the agent to stop
    using that channel. Proven against a real store + the real pattern recompute.
    """
    from prefrontal.coaching import record_channel_outcome
    from prefrontal.memory.patterns import recompute_patterns

    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        push_cue = _cue("nudge")  # floors at push
        # Before any history, a push cue stays on push.
        assert choose_channel(store, push_cue, _ctx()) == "push"
        # Five delivered push nudges, all ignored (the sweep's verdict).
        for _ in range(5):
            record_channel_outcome(store, channel="push", context="outing", acknowledged=False)
        recompute_patterns(store)
        # Now push is a channel the user reliably ignores → the cue bumps a rung.
        assert choose_channel(store, push_cue, _ctx()) == "sound"


def test_loop_acknowledged_channel_does_not_bump():
    """The mirror: a channel that gets acknowledged keeps being used."""
    from prefrontal.coaching import record_channel_outcome
    from prefrontal.memory.patterns import recompute_patterns

    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(5):
            record_channel_outcome(store, channel="push", context="outing", acknowledged=True)
        recompute_patterns(store)
        assert choose_channel(store, _cue("nudge"), _ctx()) == "push"  # no bump


def test_collect_cues_isolates_a_broken_module(caplog):
    class Boom:
        key = "boom"

        def evaluate(self, store, ctx):
            raise RuntimeError("evaluator blew up")

    class Ok:
        key = "ok"

        def evaluate(self, store, ctx):
            return [_cue()]

    with caplog.at_level("WARNING"):
        cues = collect_cues(_FakeStore(), [Boom(), Ok()], _ctx())
    assert len(cues) == 1  # the good module still contributes
    assert "boom" in caplog.text  # the swallowed failure is logged, not vanished (§8)


def test_run_module_hooks_runs_each_and_isolates_a_failure(caplog):
    """A module's lifecycle hook raising is logged and skipped, never sinking the
    tick — the same robustness collect_cues gives evaluate()."""
    from prefrontal.coaching import _run_module_hooks

    ran: list[str] = []

    class Ok:
        def __init__(self, key):
            self.key = key

        def hook(self):
            ran.append(self.key)

    class Boom:
        key = "boom"

        def hook(self):
            raise RuntimeError("hook blew up")

    with caplog.at_level("WARNING"):
        _run_module_hooks([Ok("a"), Boom(), Ok("b")], lambda m: m.hook(), "before_collect")
    assert ran == ["a", "b"]  # the good modules' hooks still ran
    assert "boom" in caplog.text and "before_collect" in caplog.text


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


# -- Location anchor evaluator (parity refactor, spec step 2) ----------------


def test_location_anchor_evaluator_emits_escalation_cue():
    """An outing over its window yields a firm→urgent cue; the one-fire level
    advance is applied in after_fire (evaluate stays a pure read)."""
    store = scoped_default(MemoryStore(init_db(":memory:")))
    oid = store.start_outing("coffee", 20.0, home_lat=0.0, home_lon=0.0)
    # Backdate departure so ~22 min have elapsed (past 100% → "firm").
    old = (utcnow() - timedelta(minutes=22)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute("UPDATE outings SET departure_at = ? WHERE id = ?", (old, oid))
    store.conn.commit()

    module = get("location_anchor")
    ctx = _ctx(now=utcnow())
    cues = module.evaluate(store, ctx)
    assert len(cues) == 1
    cue = cues[0]
    assert cue.intervention == "firm_nudge" and cue.urgency == "urgent"
    assert cue.ref == {"outing_id": oid}
    assert cue.dedup_key == f"outing_escalation:{oid}:firm"
    # evaluate is a pure read now — it hasn't advanced the level yet.
    assert store.get_outing(oid)["last_level"] == "none"
    # after_fire applies the one-fire advance, so a second tick is quiet.
    module.after_fire(store, [], ctx)
    assert store.get_outing(oid)["last_level"] == "firm"
    assert module.evaluate(store, _ctx(now=utcnow())) == []


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


def test_run_coaching_tick_service_fires_records_and_debounces():
    """The shared tick service (used by the endpoint above and the CLI) returns a
    TickResult, fires the avoided-task cue, and records it so a second tick
    debounces — this is the single orchestration both callers now delegate to."""
    from prefrontal.coaching import TickResult, run_coaching_tick
    from prefrontal.integrations.ollama import OllamaClient

    conn, unscoped = _http_store_with_avoided_todo()
    try:
        store = unscoped.scoped(unscoped.get_user("tester")["id"])
        settings = Settings(timezone="UTC")
        # A client that can't reach a model (localhost refused) → the decomposition
        # / clarification sweeps degrade to their heuristics, like the real tick.
        ollama = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
        first = run_coaching_tick(store, settings=settings, ollama=ollama)
        assert isinstance(first, TickResult)
        assert [d.cue.intervention for d in first.decisions] == ["tiny_first_step"]
        assert first.cues  # the cue was collected this tick
        # record_fired ran inside the service → a second tick debounces it.
        second = run_coaching_tick(store, settings=settings, ollama=ollama)
        assert second.decisions == []
    finally:
        conn.close()


def _tick_store_with_outing(elapsed_minutes: float, window: float = 20.0):
    """An unscoped store whose one user has one active outing, backdated so
    ``elapsed_minutes`` have elapsed. Returns ``(conn, unscoped, outing_id)``."""
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
    scoped.set_state("responsive_hours_start", "0", source="explicit")
    scoped.set_state("responsive_hours_end", "0", source="explicit")
    oid = scoped.start_outing("coffee", window, home_lat=0.0, home_lon=0.0)
    old = (utcnow() - timedelta(minutes=elapsed_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    scoped.conn.execute("UPDATE outings SET departure_at = ? WHERE id = ?", (old, oid))
    scoped.conn.commit()
    return conn, unscoped, oid


def _offline_ollama(settings):
    """A client that can't reach a model (so the tick's sweeps degrade to heuristics)."""
    from prefrontal.integrations.ollama import OllamaClient

    return OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)


def test_run_coaching_tick_applies_outing_level_advance():
    """A full tick fires the escalation cue AND applies the one-fire level advance
    (the write moved from evaluate to after_fire — this proves the tick still does it)."""
    from prefrontal.coaching import run_coaching_tick

    conn, unscoped, oid = _tick_store_with_outing(22.0)  # 110% of a 20-min window → firm
    try:
        store = unscoped.scoped(unscoped.get_user("tester")["id"])
        settings = Settings(timezone="UTC")
        result = run_coaching_tick(store, settings=settings, ollama=_offline_ollama(settings))
        outing = [d for d in result.decisions if d.cue.context_key == "outing"]
        assert outing and outing[0].cue.intervention == "firm_nudge"
        assert store.get_outing(oid)["last_level"] == "firm"  # after_fire applied it
    finally:
        conn.close()


def test_run_coaching_tick_auto_abandons_outing_without_a_cue():
    """The crux of the pure-evaluate fix: a **cue-less** transition (auto-abandon)
    still applies via after_fire — the tick closes the outing even though evaluate
    emits no cue for it, so the state change isn't silently dropped."""
    from prefrontal.coaching import run_coaching_tick

    conn, unscoped, oid = _tick_store_with_outing(120.0)  # 6× the window → abandoned
    try:
        store = unscoped.scoped(unscoped.get_user("tester")["id"])
        settings = Settings(timezone="UTC")
        result = run_coaching_tick(store, settings=settings, ollama=_offline_ollama(settings))
        assert [d for d in result.decisions if d.cue.context_key == "outing"] == []
        assert store.get_outing(oid)["status"] == "abandoned"
        assert store.active_outings() == []
    finally:
        conn.close()


def test_last_fired_reads_the_stamp_record_fired_writes():
    """The public debounce accessor a self-pacing module reads instead of the
    engine-private state-key layout: None before a fire, the stamped time after."""
    from prefrontal.coaching import last_fired, record_fired

    store = _FakeStore()
    cue = _cue(dedup="proj:7")
    assert last_fired(store, "proj:7") is None
    record_fired(store, [Decision(cue=cue, channel="push", text="x")], NOON)
    assert last_fired(store, "proj:7") == NOON


def test_channel_targets_combine_from_enabled_modules():
    """The trackable context→ref map is assembled from the modules that emit those
    cues (replacing the engine's old hardcoded table), so each declared pair shows
    up in the combined map."""
    from prefrontal.coaching import channel_targets_for
    from prefrontal.modules import enabled_modules

    combined = channel_targets_for(enabled_modules())
    assert combined["outing"] == "outing_id"        # location_anchor
    assert combined["departure"] == "commitment_id"  # time_blindness
    assert combined["focus"] == "session_id"         # hyperfocus


def test_tick_survives_a_raising_precollection_sweep(monkeypatch):
    """Failure isolation: a raise in the decomposition refresh (which runs *before*
    the guarded cue collection) must not sink the tick — it degrades to 0 and the
    rest of the tick still runs."""
    import prefrontal.todos as todos_mod
    from prefrontal.coaching import TickResult, run_coaching_tick

    def _boom(*a, **k):
        raise RuntimeError("decomposition backend down")

    monkeypatch.setattr(todos_mod, "sweep_avoided_decompositions", _boom)

    conn, unscoped, oid = _tick_store_with_outing(22.0)  # firm outing cue still due
    try:
        store = unscoped.scoped(unscoped.get_user("tester")["id"])
        settings = Settings(timezone="UTC")
        result = run_coaching_tick(store, settings=settings, ollama=_offline_ollama(settings))
        assert isinstance(result, TickResult)
        assert result.decomposed == 0  # the raising sweep degraded to 0
        # …and the tick still produced the outing escalation cue.
        assert any(d.cue.context_key == "outing" for d in result.decisions)
    finally:
        conn.close()


def test_coach_check_surfaces_morning_prep_alarm_button():
    """The evening morning-prep nudge comes back from the tick with a client-side
    Set-alarm view button (built from the cue's ref, no signing)."""
    conn = init_db(":memory:")
    try:
        unscoped = MemoryStore(conn)
        provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
        scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
        scoped.set_state("responsive_hours_start", "0", source="explicit")
        scoped.set_state("responsive_hours_end", "0", source="explicit")
        scoped.set_state("morning_prep_hour", "0")  # open the evening gate for the test
        start = (
            (utcnow() + timedelta(days=1))
            .replace(hour=8, minute=0, second=0, microsecond=0)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        scoped.upsert_commitment(title="Flight", start_at=start, lead_minutes=45.0, source="manual")
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET, timezone="UTC"))
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": SECRET}
            cues = c.post("/webhooks/coach/check", json={}, headers=hdr).json()["cues"]
            prep = next(cue for cue in cues if cue["context_key"] == "morning_prep")
            assert prep["module"] == "time_blindness"
            actions = prep["actions"]
            assert len(actions) == 1
            assert actions[0]["action"] == "view" and actions[0]["label"] == "⏰ Set alarm"
            assert actions[0]["url"].startswith("shortcuts://run-shortcut?name=Set%20Alarm")
    finally:
        conn.close()


def _http_store():
    """A bare unscoped store (for create_app) with one operator user."""
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    return conn, unscoped


def test_coach_ack_explicit_channel_logs_outcome():
    """The explicit outcome hook writes a channel_response episode (spec §8)."""
    conn, unscoped = _http_store()
    try:
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": SECRET}
            body = c.post(
                "/webhooks/coach/ack",
                json={"channel": "push", "context": "outing", "acknowledged": False},
                headers=hdr,
            ).json()
            assert body["resolved"] is True
        scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
        eps = [e for e in scoped.all_episodes() if e.get("channel") == "push"]
        assert len(eps) == 1 and eps[0]["acknowledged"] == 0  # False, stored as 0
    finally:
        conn.close()


def test_coach_ack_resolves_a_tracked_nudge_by_context_and_target():
    """The (context,target) hook resolves a delivered nudge marker and logs it."""
    conn, unscoped = _http_store()
    try:
        scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
        # Simulate a nudge the engine delivered on 'sound' for outing 7.
        note_delivered(
            scoped, [Decision(cue=_outing_cue(7), channel="sound", text="x")], NOON,
            targets=_TRACKED,
        )
        app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": SECRET}
            body = c.post(
                "/webhooks/coach/ack",
                json={"context": "outing", "target": 7},  # acknowledged defaults True
                headers=hdr,
            ).json()
            assert body["resolved"] is True
            # A duplicate/late report finds nothing left → resolved False.
            again = c.post(
                "/webhooks/coach/ack", json={"context": "outing", "target": 7}, headers=hdr
            ).json()
            assert again["resolved"] is False
        eps = [e for e in scoped.all_episodes() if e.get("channel") == "sound"]
        assert len(eps) == 1 and eps[0]["acknowledged"] == 1  # True
    finally:
        conn.close()


def test_nudge_act_tap_records_the_delivered_channel():
    """A one-tap outing action resolves the tracked nudge → a channel_response ack."""
    from prefrontal.webhooks.notify import act_url

    conn, unscoped = _http_store()
    try:
        signing = "act-signing"
        scoped = unscoped.scoped(unscoped.get_user("tester")["id"])
        oid = scoped.start_outing(intention="Coffee", time_window_minutes=30)
        note_delivered(
            scoped, [Decision(cue=_outing_cue(oid), channel="sound", text="x")], NOON,
            targets=_TRACKED,
        )
        settings = Settings(webhook_secret=SECRET, session_secret=signing,
                            oauth_base_url="https://x.ts.net")
        app = create_app(store=unscoped, settings=settings)
        with TestClient(app) as c:
            url = act_url("https://x.ts.net", "tester", "outing_return", oid, signing)
            token = url.split("t=", 1)[1]
            assert c.get("/nudge/act", params={"t": token}).status_code == 200
        eps = [e for e in scoped.all_episodes() if e.get("channel") == "sound"]
        assert len(eps) == 1 and eps[0]["acknowledged"] == 1  # tap = acknowledged
    finally:
        conn.close()


# -- responsive hours accept "HH:MM" as well as bare hours -------------------


def test_responsive_hours_honor_hh_mm_clock_strings():
    """Regression: a window stored as "08:00"/"14:00" was silently dropped.

    ``float("08:00")`` raises, so the old ``int(get_float(...))`` fell back to
    the 8–22 defaults — the user's "quiet after 2pm" never took effect. Both
    ``build_context`` and ``in_quiet_hours`` now parse the clock string.
    """
    store = scoped_default(MemoryStore(init_db(":memory:")))
    store.set_state("responsive_hours_start", "08:00", source="explicit")
    store.set_state("responsive_hours_end", "14:00", source="explicit")

    ctx = build_context(store, now=NOON)
    assert (ctx.responsive_start, ctx.responsive_end) == (8, 14)

    # 15:00 UTC is past the 14:00 end → outside the window (quiet).
    assert in_quiet_hours(store, datetime(2026, 7, 2, 15, 0, 0), "UTC") is True
    # 12:00 UTC is inside 08:00–14:00 → responsive.
    assert in_quiet_hours(store, datetime(2026, 7, 2, 12, 0, 0), "UTC") is False


def test_responsive_hours_still_accept_bare_integer_hours():
    store = scoped_default(MemoryStore(init_db(":memory:")))
    store.set_state("responsive_hours_start", "9", source="explicit")
    store.set_state("responsive_hours_end", "17", source="explicit")
    ctx = build_context(store, now=NOON)
    assert (ctx.responsive_start, ctx.responsive_end) == (9, 17)


def test_get_hour_accessor_parses_both_forms_and_falls_back():
    store = scoped_default(MemoryStore(init_db(":memory:")))
    store.set_state("meal_start_hour", "08:00", source="explicit")
    assert store.get_hour("meal_start_hour", 11) == 8
    store.set_state("meal_start_hour", "13", source="explicit")
    assert store.get_hour("meal_start_hour", 11) == 13
    store.set_state("meal_start_hour", "garbage", source="explicit")
    assert store.get_hour("meal_start_hour", 11) == 11  # unparseable → default
    assert store.get_hour("never_set_key", 9) == 9  # absent → default


# -- LLM phrasing pass (spec §5) ---------------------------------------------


class _FakeClient:
    """Stub model client: capture the system prompt, return a canned reply."""

    def __init__(self, reply="", model="fake-model", error=False):
        self.reply = reply
        self.model = model
        self.error = error
        self.system = None

    def generate(self, prompt, *, system=None):
        from prefrontal.integrations.ollama import OllamaError

        if self.error:
            raise OllamaError("model down")
        self.system = system
        return self.reply


def _text_cue(urgency, text, dedup="k") -> Cue:
    return Cue(
        module="m", intervention="i", urgency=urgency, text=text,
        context_key="todo", dedup_key=dedup,
    )


def test_phrase_returns_cue_text_when_phrasing_off():
    """Off by default: even an ambient cue keeps its deterministic text."""
    store = _FakeStore()
    cue = _text_cue("ambient", "you've been out a while")
    assert phrase(store, cue, _ctx(), client=_FakeClient(reply="warm")) == "you've been out a while"


def test_phrase_leaves_time_critical_cues_deterministic_even_when_on():
    """A nudge is never rewritten — latency on a time-critical path is bad (§13)."""
    store = _FakeStore(state={"coach_llm_phrasing": "on"})
    cue = _text_cue("nudge", "leave now for the dentist")
    fake = _FakeClient(reply="Hey, time to head out!")
    assert phrase(store, cue, _ctx(), client=fake) == "leave now for the dentist"
    assert fake.system is None  # the model was never called


def test_phrase_rewrites_ambient_when_on_and_grounds_in_profile():
    store = _FakeStore(state={"coach_llm_phrasing": "on"})
    cue = _text_cue("ambient", "you've been out a while")
    fake = _FakeClient(reply="You've been out a bit — all good, just a heads up.")
    out = phrase(store, cue, _ctx(), client=fake, profile="pads estimates 1.4x")
    assert out == "You've been out a bit — all good, just a heads up."
    assert "pads estimates 1.4x" in fake.system  # profile threaded into the system prompt


def test_phrase_falls_back_to_cue_text_when_model_errors():
    store = _FakeStore(state={"coach_llm_phrasing": "on"})
    cue = _text_cue("ambient", "you've been out a while")
    assert phrase(store, cue, _ctx(), client=_FakeClient(error=True)) == "you've been out a while"


def test_decide_phrases_ambient_cue_through_the_client():
    store = _FakeStore(state={"coach_llm_phrasing": "on"})
    cue = _text_cue("ambient", "raw ambient text", dedup="amb")
    fake = _FakeClient(reply="warmed ambient text")
    decisions = decide(store, [cue], _ctx(), client=fake, profile="prof")
    assert len(decisions) == 1
    assert decisions[0].channel == "digest" and decisions[0].text == "warmed ambient text"


# -- outing/check deprecation (spec §13) -------------------------------------


def test_outing_check_is_marked_deprecated_in_the_schema():
    """coach/check now runs the identical anchor decision, so outing/check is
    flagged deprecated (kept for existing n8n workflows)."""
    conn = init_db(":memory:")
    try:
        app = create_app(store=MemoryStore(conn), settings=Settings(webhook_secret=SECRET))
        spec = app.openapi()["paths"]["/webhooks/outing/check"]["post"]
        assert spec.get("deprecated") is True
    finally:
        conn.close()
