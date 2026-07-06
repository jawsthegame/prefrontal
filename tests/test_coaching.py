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
    in_quiet_hours,
    note_delivered,
    record_fired,
    resolve_ack,
    suppressed,
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
    note_delivered(store, decisions, NOON)
    keys = sorted(k for k in store._state if k.startswith("coach_ack:"))
    assert keys == ["coach_ack:outing:7"]  # only the interactive, non-digest cue
    assert store._state["coach_ack:outing:7"].split("|")[1] == "push"


def test_resolve_ack_logs_acknowledged_and_clears_marker():
    store = _FakeStore()
    note_delivered(store, [Decision(cue=_outing_cue(7), channel="sound", text="x")], NOON)
    assert resolve_ack(store, "outing", 7) is True
    assert store.episodes == [{
        "episode_type": "reminder", "channel": "sound", "acknowledged": True,
        "context": "coach nudge: outing", "outcome": "success",
    }]
    assert "coach_ack:outing:7" not in store._state       # marker cleared
    assert resolve_ack(store, "outing", 7) is False        # nothing left → no-op


def test_sweep_logs_unacked_as_miss_after_window_but_leaves_fresh_ones():
    store = _FakeStore()
    note_delivered(store, [Decision(cue=_outing_cue(1), channel="push", text="x")], NOON)
    note_delivered(
        store, [Decision(cue=_outing_cue(2), channel="push", text="y")],
        NOON - timedelta(minutes=90),  # delivered long ago → past the ack window
    )
    swept = sweep_stale_nudges(store, NOON, ack_window_minutes=60.0)
    assert swept == 1  # only the stale one
    assert store.episodes == [{
        "episode_type": "reminder", "channel": "push", "acknowledged": False,
        "context": "coach nudge: outing", "outcome": "miss",
    }]
    assert "coach_ack:outing:2" not in store._state   # swept marker cleared
    assert "coach_ack:outing:1" in store._state        # fresh one still pending


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
    """An outing over its window yields a firm→urgent cue and advances last_level."""
    store = scoped_default(MemoryStore(init_db(":memory:")))
    oid = store.start_outing("coffee", 20.0, home_lat=0.0, home_lon=0.0)
    # Backdate departure so ~22 min have elapsed (past 100% → "firm").
    old = (utcnow() - timedelta(minutes=22)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute("UPDATE outings SET departure_at = ? WHERE id = ?", (old, oid))
    store.conn.commit()

    cues = get("location_anchor").evaluate(store, _ctx(now=utcnow()))
    assert len(cues) == 1
    cue = cues[0]
    assert cue.intervention == "firm_nudge" and cue.urgency == "urgent"
    assert cue.ref == {"outing_id": oid}
    assert cue.dedup_key == f"outing_escalation:{oid}:firm"
    # The shared side-effect advanced the one-fire level, so a second tick is quiet.
    assert store.get_outing(oid)["last_level"] == "firm"
    assert get("location_anchor").evaluate(store, _ctx(now=utcnow())) == []


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
            scoped, [Decision(cue=_outing_cue(7), channel="sound", text="x")], NOON
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
            scoped, [Decision(cue=_outing_cue(oid), channel="sound", text="x")], NOON
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
