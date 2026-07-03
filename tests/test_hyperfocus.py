"""Tests for the Hyperfocus module's focus-session primitive.

Covers the pure interrupt/protect logic, the ``focus_sessions`` store methods,
and the ``/webhooks/focus/{start,check,end}`` + ``/focus`` endpoints (including
that each interrupt level fires exactly once and that aligned blocks are
protected until the hard ceiling).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.coaching import CoachContext
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.hyperfocus import (
    HyperfocusModule,
    build_focus_message,
    focus_level,
    focus_task_from_title,
    infer_focus_start,
    is_abandoned,
    is_focus_intent_title,
    is_focus_protected,
    level_rank,
    should_protect,
    should_recap_focus,
    should_suggest_focus,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "focus-secret"


def _utc_minutes_ago(minutes: float) -> str:
    """A SQLite-style UTC timestamp `minutes` in the past (for started_at)."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


# -- pure logic --------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,planned,expected",
    [
        (10, None, "none"),       # early, no plan -> below the soft block
        (95, None, "check"),      # past the 90-min soft-block default
        (35, 30, "check"),        # past the *planned* 30 min
        (20, 30, "none"),         # still inside the planned window
        (200, 30, "break"),       # past the hard ceiling regardless of plan
        (200, None, "break"),     # hard ceiling dominates
    ],
)
def test_focus_level(elapsed, planned, expected):
    assert focus_level(elapsed, planned_minutes=planned) == expected


def test_focus_level_respects_a_long_plan():
    """A declared 4-hour block is not 'check' at the 90-min default."""
    assert focus_level(100, planned_minutes=240) == "none"


def test_level_rank_orders_severity():
    assert level_rank("none") < level_rank("check") < level_rank("break")


@pytest.mark.parametrize(
    "level,aligned,enabled,expected",
    [
        ("none", True, True, True),    # aligned + healthy -> protect
        ("check", True, True, True),   # still protected during an overrun
        ("break", True, True, False),  # the biological break lifts protection
        ("none", False, True, False),  # misaligned -> never protected
        ("none", True, False, False),  # preference disabled
    ],
)
def test_should_protect(level, aligned, enabled, expected):
    assert should_protect(level, aligned=aligned, protect_enabled=enabled) is expected


def test_is_abandoned():
    # Default ceiling 180, ratio 2.0 -> abandoned at 360 min.
    assert not is_abandoned(300, 180, 2.0)
    assert is_abandoned(360, 180, 2.0)
    assert not is_abandoned(100, 0, 2.0)  # no ceiling -> never abandoned


def test_build_focus_message_varies_by_level_and_alignment():
    aligned = build_focus_message("check", task="the refactor", elapsed_minutes=95, aligned=True)
    assert "Still what you meant" in aligned and "the refactor" in aligned
    # Points at the real one-tap Wrap up button (ntfy), not a phantom tap action.
    assert "Wrap up" in aligned and "Tap to" not in aligned
    redirect = build_focus_message("check", task="email", elapsed_minutes=95, aligned=False)
    assert "park it" in redirect
    brk = build_focus_message("break", task="x", elapsed_minutes=200, name="Tom")
    assert brk.startswith("Hey Tom") and "Stand up" in brk
    assert build_focus_message("none", task="x", elapsed_minutes=5) == ""


def test_is_focus_protected_reflects_active_sessions():
    """The live helper protects an aligned healthy block and not a misaligned one."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        assert is_focus_protected(store) is False  # nothing active
        store.start_focus_session("deep work", started_at=_utc_minutes_ago(30))
        assert is_focus_protected(store) is True
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.start_focus_session(
            "doomscrolling", aligned=False, started_at=_utc_minutes_ago(30)
        )
        assert is_focus_protected(store) is False


# -- store -------------------------------------------------------------------


def test_focus_session_store_lifecycle():
    """Start -> active (with elapsed) -> close (with actual minutes + breadcrumb)."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        sid = store.start_focus_session(
            "the API refactor", planned_minutes=60, started_at=_utc_minutes_ago(12)
        )
        active = store.active_focus_sessions()
        assert len(active) == 1
        assert active[0]["id"] == sid
        assert active[0]["aligned"] == 1
        assert active[0]["elapsed_minutes"] == pytest.approx(12, abs=0.5)

        closed = store.close_focus_session(
            sid, breadcrumb="finish the migration", outcome="worth_it"
        )
        assert closed["status"] == "ended"
        assert closed["actual_minutes"] == pytest.approx(12, abs=0.5)
        assert closed["breadcrumb"] == "finish the migration"
        assert closed["outcome"] == "worth_it"
        # No longer active; double-close is a no-op.
        assert store.active_focus_sessions() == []
        assert store.close_focus_session(sid) is None


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store():
    """An in-memory store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    """A TestClient wired to the injected store with auth enabled."""
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_start_creates_session(client):
    resp = client.post(
        "/webhooks/focus/start",
        json={"intended_task": "the API refactor", "planned_minutes": 90},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == 1
    assert body["intended_task"] == "the API refactor"
    assert body["planned_minutes"] == 90
    assert body["aligned"] is True


def test_infer_focus_start_picks_first_titled_todo():
    """The first candidate with a real title wins, carrying its estimate."""
    got = infer_focus_start([
        {"id": 1, "title": "   "},          # blank — skipped
        {"id": 2, "title": "write the RFC", "estimate_minutes": 60},
        {"id": 3, "title": "later"},
    ])
    assert (got.intended_task, got.planned_minutes, got.source) == ("write the RFC", 60.0, "todo")


def test_infer_focus_start_no_estimate_leaves_planned_none():
    got = infer_focus_start([{"id": 1, "title": "read the spec"}])
    assert got.planned_minutes is None and got.source == "todo"


def test_infer_focus_start_falls_back_to_generic_block():
    got = infer_focus_start([])
    assert got.intended_task == "a focus block"
    assert got.planned_minutes is None and got.source == "default"


def test_infer_focus_start_links_todo_id():
    """The picked todo's id rides along so the session tags it for learning."""
    got = infer_focus_start([{"id": 7, "title": "ship it", "estimate_minutes": 30}])
    assert got.todo_id == 7
    assert infer_focus_start([]).todo_id is None


@pytest.mark.parametrize(
    "title, intent, task",
    [
        ("Deep work: the RFC", True, "the RFC"),
        ("Deep-Work — auth refactor", True, "auth refactor"),
        ("Focus time", True, None),
        ("Focus", True, None),
        ("heads-down", True, None),
        ("Team standup", False, "Team standup"),
    ],
)
def test_focus_intent_title_parsing(title, intent, task):
    assert is_focus_intent_title(title) is intent
    if intent:
        assert focus_task_from_title(title) == task


def test_start_blank_infers_generic_block_when_no_todos(client):
    """A one-tap start with nothing open still works — a generic block."""
    resp = client.post(
        "/webhooks/focus/start", json={"intended_task": "   "}, headers=_auth()
    )
    assert resp.status_code == 201
    assert resp.json()["intended_task"] == "a focus block"


def test_start_blank_infers_top_todo(client, store):
    """A one-tap start (no fields) picks the top open todo + its estimate."""
    store.add_todo("the API refactor", estimate_minutes=45, priority=2)
    resp = client.post("/webhooks/focus/start", json={}, headers=_auth())
    assert resp.status_code == 201
    body = resp.json()
    assert body["intended_task"] == "the API refactor"
    assert body["planned_minutes"] == 45
    assert "open todos" in body["confirmation"]


def test_one_tap_links_inferred_todo_id(client, store):
    """A one-tap start links the todo it picked, so learning can tag the block."""
    tid = store.add_todo("the API refactor", estimate_minutes=45, priority=2)
    body = client.post("/webhooks/focus/start", json={}, headers=_auth()).json()
    assert store.get_focus_session(body["session_id"])["todo_id"] == tid


# -- proactive "want to focus?" suggestion -----------------------------------


@pytest.mark.parametrize(
    "hour, active, done_today, expected",
    [
        (9, False, False, True),    # ripe: morning, nothing running, none yet today
        (9, True, False, False),    # a session is already running
        (9, False, True, False),    # already focused today
        (20, False, False, False),  # outside the focus-friendly band
        (7, False, False, False),   # before the band
    ],
)
def test_should_suggest_focus(hour, active, done_today, expected):
    assert should_suggest_focus(
        local_hour=hour, start_hour=8, until_hour=15,
        has_active_session=active, focused_today=done_today,
    ) is expected


def _ctx(hour):
    return CoachContext(now=datetime(2026, 6, 15, hour, 0, 0), timezone="UTC")


def test_evaluate_suggests_when_opted_in(store):
    """Opted in, a fresh morning tick offers a one-tap start (once/day dedup)."""
    store.set_state("suggest_focus_start", "on", source="explicit")
    cues = HyperfocusModule().evaluate(store, _ctx(9))
    assert len(cues) == 1
    assert cues[0].intervention == "suggest_start"
    assert cues[0].dedup_key == "focus_suggest:2026-06-15"


def test_evaluate_silent_by_default(store):
    """Off by default — no unsolicited 'go focus' nudge."""
    assert HyperfocusModule().evaluate(store, _ctx(9)) == []


def test_evaluate_silent_when_session_active(store):
    """Never suggest starting while a block is already running."""
    store.set_state("suggest_focus_start", "on", source="explicit")
    store.start_focus_session("deep in it")
    assert HyperfocusModule().evaluate(store, _ctx(9)) == []


@pytest.mark.parametrize(
    "hour, done_today, expected",
    [
        (19, False, True),    # evening, nothing logged -> ask
        (19, True, False),    # a block was logged today -> nothing to recover
        (12, False, False),   # midday -> not the recap window
    ],
)
def test_should_recap_focus(hour, done_today, expected):
    assert should_recap_focus(
        local_hour=hour, start_hour=17, until_hour=22,
        has_active_session=False, focused_today=done_today,
    ) is expected


def test_evaluate_evening_recap_when_opted_in(store):
    """Opted in, an evening with no logged block offers to log one after the fact."""
    store.set_state("suggest_focus_recap", "on", source="explicit")
    cues = HyperfocusModule().evaluate(store, _ctx(19))
    assert len(cues) == 1
    assert cues[0].intervention == "recap_prompt"
    assert cues[0].dedup_key == "focus_recap:2026-06-15"


def test_evaluate_recap_silent_after_a_logged_block(store):
    """If a block already happened today, the recap stays quiet."""
    store.set_state("suggest_focus_recap", "on", source="explicit")
    sid = store.start_focus_session("the RFC", started_at="2026-06-15 10:00:00")
    store.close_focus_session(sid, status="ended", outcome="worth_it")
    assert HyperfocusModule().evaluate(store, _ctx(19)) == []


# -- retroactive capture (log a block you forgot) ----------------------------


def test_focus_log_records_a_past_session(client, store):
    """Logging a forgotten block records it as ended, with the actual duration."""
    body = client.post(
        "/webhooks/focus/log",
        json={"minutes": 60, "intended_task": "the RFC", "outcome": "worth_it"},
        headers=_auth(),
    ).json()
    assert body["logged"] is True and body["intended_task"] == "the RFC"
    assert 59 <= body["minutes"] <= 61
    session = store.get_focus_session(body["session_id"])
    assert session["status"] == "ended"
    # It fed the learning loop as a task episode (actual duration, no prediction).
    episodes = store.episodes_by_type("task")
    assert episodes and episodes[0]["actual_value"] == pytest.approx(60, abs=1)


def test_focus_log_infers_task_and_links_todo(client, store):
    """A blank log infers the task from the top todo and links it."""
    tid = store.add_todo("the API refactor", estimate_minutes=45, priority=2)
    body = client.post("/webhooks/focus/log", json={"minutes": 30}, headers=_auth()).json()
    assert body["intended_task"] == "the API refactor"
    assert store.get_focus_session(body["session_id"])["todo_id"] == tid


# -- calendar-armed start ----------------------------------------------------

_NOW = datetime(2026, 6, 15, 13, 0, 0)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _freeze_focus_clock(monkeypatch):
    monkeypatch.setattr("prefrontal.webhooks.routers.focus.utcnow", lambda: _NOW)


def test_arm_starts_from_live_focus_event(client, store, monkeypatch):
    """A live 'Deep work: X' block arms a protected session for its remaining window."""
    _freeze_focus_clock(monkeypatch)
    store.upsert_commitment(
        title="Deep work: the RFC",
        start_at=_ts(_NOW - timedelta(minutes=30)),
        end_at=_ts(_NOW + timedelta(minutes=60)),
        hardness="soft",
    )
    body = client.post("/webhooks/focus/arm", headers=_auth()).json()
    assert body["armed"] is True
    assert body["intended_task"] == "the RFC"
    assert 59 <= body["planned_minutes"] <= 61  # ~remaining
    assert store.get_focus_session(body["session_id"])["intended_task"] == "the RFC"


def test_arm_bare_focus_time_protects_top_todo(client, store, monkeypatch):
    """A label-only 'Focus time' block falls back to protecting your top todo."""
    _freeze_focus_clock(monkeypatch)
    tid = store.add_todo("the API refactor", estimate_minutes=45, priority=2)
    store.upsert_commitment(
        title="Focus time",
        start_at=_ts(_NOW - timedelta(minutes=10)),
        end_at=_ts(_NOW + timedelta(minutes=50)),
        hardness="soft",
    )
    body = client.post("/webhooks/focus/arm", headers=_auth()).json()
    assert body["armed"] is True and body["intended_task"] == "the API refactor"
    assert store.get_focus_session(body["session_id"])["todo_id"] == tid


def test_arm_skips_when_session_active(client, store, monkeypatch):
    """Never stack a second session on top of one that's already running."""
    _freeze_focus_clock(monkeypatch)
    store.start_focus_session("already going")
    store.upsert_commitment(
        title="Deep work: x",
        start_at=_ts(_NOW - timedelta(minutes=5)),
        end_at=_ts(_NOW + timedelta(minutes=30)),
        hardness="soft",
    )
    body = client.post("/webhooks/focus/arm", headers=_auth()).json()
    assert body["armed"] is False and "active" in body["reason"]


def test_arm_skips_when_no_live_block(client, store, monkeypatch):
    """A focus block that already ended doesn't arm anything."""
    _freeze_focus_clock(monkeypatch)
    store.upsert_commitment(
        title="Deep work: earlier",
        start_at=_ts(_NOW - timedelta(hours=3)),
        end_at=_ts(_NOW - timedelta(hours=2)),
        hardness="soft",
    )
    body = client.post("/webhooks/focus/arm", headers=_auth()).json()
    assert body["armed"] is False


def test_start_confirmation_notes_protection_and_plan(client):
    """An aligned, planned session reads back as protected with its length."""
    body = client.post(
        "/webhooks/focus/start",
        json={"intended_task": "the API refactor", "planned_minutes": 90},
        headers=_auth(),
    ).json()
    conf = body["confirmation"]
    assert "the API refactor" in conf
    assert "90 min" in conf
    assert "protected" in conf


def test_end_confirmation_reports_time(store, client):
    """Ending reads back actual vs planned for a thin client to show verbatim."""
    sid = store.start_focus_session(
        "the refactor", planned_minutes=60, started_at=_utc_minutes_ago(75)
    )
    body = client.post(
        "/webhooks/focus/end", json={"session_id": sid}, headers=_auth()
    ).json()
    conf = body["confirmation"]
    assert "Focus ended" in conf
    assert "planned 60" in conf


def test_check_protects_aligned_block(store, client):
    """An aligned block inside its plan fires nothing and reports protect=true."""
    store.start_focus_session(
        "deep work", planned_minutes=120, started_at=_utc_minutes_ago(30)
    )
    resp = client.post("/webhooks/focus/check", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["protect"] is True
    item = body["active"][0]
    assert item["level"] == "none"
    assert item["fire"] is False
    assert item["protect"] is True


def test_check_fires_each_level_once(store, client):
    """A new level fires once; an immediate re-poll does not refire it."""
    # Past the 90-min default soft block, below the 180-min ceiling -> 'check'.
    store.start_focus_session("a tangent", started_at=_utc_minutes_ago(100))
    first = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert first["level"] == "check" and first["fire"] is True
    assert first["message"]
    second = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert second["level"] == "check" and second["fire"] is False


def test_check_break_fires_and_is_not_protected(store, client):
    """Past the hard ceiling a break fires and protection lifts even when aligned."""
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(200))
    body = client.post("/webhooks/focus/check", headers=_auth()).json()
    item = body["active"][0]
    assert item["level"] == "break" and item["fire"] is True
    assert item["protect"] is False
    assert body["protect"] is False


def test_check_abandoned_auto_closes(store, client):
    """A session left open far past the ceiling auto-closes as abandoned."""
    # Default ceiling 180 * ratio 2.0 = 360 min.
    sid = store.start_focus_session("forgotten", started_at=_utc_minutes_ago(400))
    item = client.post("/webhooks/focus/check", headers=_auth()).json()["active"][0]
    assert item["status"] == "abandoned"
    assert store.get_focus_session(sid)["status"] == "abandoned"


def test_end_logs_episode_with_outcome(store, client):
    """Ending a session logs a task episode and persists breadcrumb + rating."""
    sid = store.start_focus_session(
        "the refactor", planned_minutes=60, started_at=_utc_minutes_ago(75)
    )
    resp = client.post(
        "/webhooks/focus/end",
        json={
            "session_id": sid,
            "outcome": "worth_it",
            "breadcrumb": "wire up the poller next",
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ended"
    assert body["actual_minutes"] == pytest.approx(75, abs=0.5)
    assert body["breadcrumb"] == "wire up the poller next"
    assert body["outcome"] == "success"  # worth_it -> success
    episode = store.get_episode(body["episode_id"])
    assert episode["episode_type"] == "task"
    assert episode["predicted_value"] == 60
    assert episode["outcome"] == "success"
    assert "next: wire up the poller next" in episode["notes"]


def test_end_stamps_linked_todo_energy_and_category(store, client):
    """A session started with a todo_id tags its close episode with the todo's
    energy/category, so the bias can condition on them (learning §5)."""
    tid = store.add_todo("prep the deck", category="work", energy="high")
    start = client.post(
        "/webhooks/focus/start",
        json={"intended_task": "prep the deck", "planned_minutes": 45, "todo_id": tid},
        headers=_auth(),
    )
    assert start.status_code == 201
    sid = start.json()["session_id"]
    end = client.post("/webhooks/focus/end", json={"session_id": sid}, headers=_auth())
    episode = store.get_episode(end.json()["episode_id"])
    assert episode["energy"] == "high"
    assert episode["category"] == "work"


def test_end_without_todo_link_leaves_tags_null(store, client):
    """A free-text session (no todo_id) contributes untagged, exactly as before."""
    sid = store.start_focus_session("free-text focus", started_at=_utc_minutes_ago(20))
    end = client.post("/webhooks/focus/end", json={"session_id": sid}, headers=_auth())
    episode = store.get_episode(end.json()["episode_id"])
    assert episode["energy"] is None
    assert episode["category"] is None


def test_end_defaults_to_most_recent_active(store, client):
    store.start_focus_session("first", started_at=_utc_minutes_ago(20))
    sid2 = store.start_focus_session("second", started_at=_utc_minutes_ago(5))
    body = client.post("/webhooks/focus/end", json={}, headers=_auth()).json()
    assert body["session_id"] == sid2


def test_end_without_active_session_is_404(client):
    resp = client.post("/webhooks/focus/end", json={}, headers=_auth())
    assert resp.status_code == 404


def test_focus_list_reports_active_and_recent(store, client):
    store.start_focus_session("ongoing", planned_minutes=120, started_at=_utc_minutes_ago(30))
    done = store.start_focus_session("done", started_at=_utc_minutes_ago(60))
    store.close_focus_session(done, outcome="should_have_stopped")
    body = client.get("/focus", headers=_auth()).json()
    assert len(body["active"]) == 1
    assert body["active"][0]["intended_task"] == "ongoing"
    assert body["active"][0]["protect"] is True
    assert any(r["intended_task"] == "done" for r in body["recent"])


def test_focus_list_has_no_side_effects(store, client):
    """GET /focus never fires or records a level (unlike /check)."""
    sid = store.start_focus_session("a tangent", started_at=_utc_minutes_ago(100))
    client.get("/focus", headers=_auth())
    assert store.get_focus_session(sid)["last_level"] == "none"


def test_focus_endpoints_require_auth(client):
    assert client.post("/webhooks/focus/start", json={"intended_task": "x"}).status_code == 401
    assert client.post("/webhooks/focus/check").status_code == 401
    assert client.post("/webhooks/focus/end", json={}).status_code == 401
    assert client.get("/focus").status_code == 401
