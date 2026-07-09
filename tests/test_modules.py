"""Tests for the challenge-area module system.

Covers the registry (availability, enable/disable via settings), per-module
seeding and profile contributions, and the summarizer's integration of module
sections. Built-in modules are imported for their registration side effects.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import prefrontal.modules  # noqa: F401  (registers built-in modules on import)
from prefrontal.coaching import CoachContext
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile
from prefrontal.modules import available, enabled_modules, get
from prefrontal.modules.task_paralysis import repeat_stalled_tasks
from prefrontal.modules.time_blindness import TimeBlindnessModule
from tests.conftest import scoped_default

BUILTIN_KEYS = {"time_blindness", "task_paralysis", "hyperfocus", "impulsivity"}


@pytest.fixture()
def store():
    """Yield a MemoryStore backed by a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def test_all_builtins_registered():
    """The four built-in modules are all registered."""
    keys = {m.key for m in available()}
    assert BUILTIN_KEYS <= keys


def test_empty_settings_enables_everything():
    """No explicit module list means every module is enabled."""
    enabled = {m.key for m in enabled_modules(Settings(modules=()))}
    assert BUILTIN_KEYS <= enabled


def test_explicit_list_filters_and_orders():
    """An explicit list enables only those modules, in order, ignoring unknowns."""
    settings = Settings(modules=("hyperfocus", "bogus", "time_blindness"))
    enabled = [m.key for m in enabled_modules(settings)]
    assert enabled == ["hyperfocus", "time_blindness"]


def test_module_seed_is_non_clobbering(store):
    """seed() writes its defaults but never overwrites an existing value."""
    store.set_state("departure_buffer_minutes", "25", source="explicit")
    get("time_blindness").seed(store)
    # Existing explicit value preserved...
    assert store.get_state("departure_buffer_minutes") == "25"
    # ...but a missing default from the same module is now present.
    assert store.get_state("time_estimation_bias") is not None


def test_each_module_declares_interventions():
    """Every built-in module declares at least one intervention."""
    for module in available():
        assert module.interventions(), f"{module.key} has no interventions"


def test_hyperfocus_profile_mentions_good_vs_bad(store):
    """Hyperfocus is the asymmetric case — its profile must say so."""
    get("hyperfocus").seed(store)
    section = get("hyperfocus").profile_section(store)
    assert section is not None
    assert "good vs bad" in section.lower()


def test_profile_includes_module_sections(store):
    """build_profile renders an Active modules section with module titles."""
    for module in available():
        module.seed(store)
    profile = build_profile(store, modules=available())
    assert "## Active modules" in profile
    assert "### Time Blindness" in profile
    assert "### Hyperfocus" in profile


def test_profile_can_omit_modules(store):
    """Passing an empty module list omits the module sections."""
    profile = build_profile(store, modules=[])
    assert "## Active modules" not in profile


def test_task_paralysis_reports_stall_rate(store):
    """With mostly-missed tasks, the module reports a high stall rate."""
    for _ in range(3):
        store.log_episode("task", acknowledged=False, outcome="miss")
    store.log_episode("task", acknowledged=True, outcome="success")
    get("task_paralysis").seed(store)
    section = get("task_paralysis").profile_section(store)
    assert section is not None
    assert "stall" in section.lower()


def test_task_paralysis_interventions_all_active():
    """All initiation interventions are wired (status active)."""
    ivs = {i.name: i.status for i in get("task_paralysis").interventions()}
    assert ivs == {
        "tiny_first_step": "active",
        "auto_decompose": "active",
        "body_double_nudge": "active",
        "clarify_ambiguous": "active",
        "refocus": "active",
    }


def _age_todo(store, todo_id, days):
    """Backdate a todo's created_at so it clears the avoidance floor."""
    ts = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute(
        "UPDATE todos SET created_at = ? WHERE id = ?", (ts, todo_id)
    )
    store.conn.commit()


def test_task_paralysis_refocus_fires_on_focus_conflict(store):
    """Working a low-priority todo while an important one is avoided → a refocus cue."""
    important = store.add_todo("Renew passport", estimate_minutes=20, priority=3)
    _age_todo(store, important, 8)          # aged into "avoided", not started
    minor = store.add_todo("Tidy desk", estimate_minutes=10, priority=1)
    store.start_todo(minor)                 # you're mid-task on the minor one

    cues = get("task_paralysis").evaluate(store, CoachContext(now=utcnow()))
    assert len(cues) == 1
    cue = cues[0]
    assert cue.intervention == "refocus"
    assert "Renew passport" in cue.text and "Tidy desk" in cue.text
    assert cue.ref == {"todo_id": important, "working_on_id": minor}
    # Deduped per (working, instead) pair so it fires once per distinct conflict.
    assert cue.dedup_key == f"refocus:{minor}:{important}"


def test_task_paralysis_refocus_held_by_engine_during_protected_focus(store):
    """An aligned focus block shields the user from refocus — but the hold now lives
    in the engine's central suppression gate, not the module. The module emits
    refocus regardless; the engine holds it while the block is protected."""
    from prefrontal.coaching import decide, suppressed
    from prefrontal.modules.hyperfocus import is_focus_protected

    important = store.add_todo("Renew passport", estimate_minutes=20, priority=3)
    _age_todo(store, important, 8)
    minor = store.add_todo("Tidy desk", estimate_minutes=10, priority=1)
    store.start_todo(minor)
    store.start_focus_session("deep work", aligned=True)  # protected hyperfocus
    assert is_focus_protected(store) is True  # the tick reads this into ctx

    # The module no longer self-checks protection: it emits refocus either way.
    cues = get("task_paralysis").evaluate(store, CoachContext(now=utcnow()))
    assert [c.intervention for c in cues] == ["refocus"]

    # Always-responsive window so only the protection gate is in play.
    protected = CoachContext(
        now=utcnow(), responsive_start=0, responsive_end=0, focus_protected=True
    )
    assert suppressed(store, cues[0], protected) is True
    assert decide(store, cues, protected) == []
    # Once the block is no longer protected, the same cue is let through.
    open_ctx = CoachContext(
        now=utcnow(), responsive_start=0, responsive_end=0, focus_protected=False
    )
    assert suppressed(store, cues[0], open_ctx) is False


def test_protected_focus_gate_exempts_self_care_and_critical(store):
    """The central protection gate holds non-critical cues from other modules, but
    self-care pierces flow (eat/drink) and critical always lands."""
    from prefrontal.coaching import Cue, suppressed

    ctx = CoachContext(
        now=utcnow(), responsive_start=0, responsive_end=0, focus_protected=True
    )

    def cue(module: str, urgency: str) -> Cue:
        return Cue(
            module=module, intervention="x", urgency=urgency, text="t",
            context_key="c", dedup_key=f"{module}:{urgency}",
        )

    assert suppressed(store, cue("task_paralysis", "nudge"), ctx) is True
    assert suppressed(store, cue("time_blindness", "urgent"), ctx) is True
    assert suppressed(store, cue("self_care", "nudge"), ctx) is False
    assert suppressed(store, cue("location_anchor", "critical"), ctx) is False


def test_time_blindness_intervention_statuses_are_honest():
    """All the time-awareness interventions are wired.

    The status flags must reflect reality (they drive ``prefrontal modules -v``):
    ``departure_buffer`` is delivered by the live departure planner
    (bias-adjusted leave-by + heads_up/soon/go escalation, ``/webhooks/departure``),
    ``elapsed_time_callouts`` fires from :meth:`TimeBlindnessModule.evaluate`
    on the coaching tick (opt-in via ``elapsed_callout_minutes``), and
    ``morning_prep`` sends the evening "early start tomorrow — set an alarm"
    heads-up off the same tick.
    """
    ivs = {i.name: i.status for i in get("time_blindness").interventions()}
    assert ivs == {
        "estimate_correction": "active",
        "departure_buffer": "active",
        "elapsed_time_callouts": "active",
        "morning_prep": "active",
    }


def _started_ago(minutes: float) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_elapsed_callouts_off_by_default(store):
    """No callout unless the user opts in — even with an active session running."""
    store.start_focus_session("the refactor", started_at=_started_ago(65))
    assert TimeBlindnessModule().evaluate(store, CoachContext(now=utcnow())) == []


def test_elapsed_callouts_fire_per_bucket_when_enabled(store):
    store.set_state("elapsed_callout_minutes", "30")
    store.start_focus_session("the refactor", started_at=_started_ago(65))
    ctx = CoachContext(now=utcnow(), display_name="Tom")
    cues = TimeBlindnessModule().evaluate(store, ctx)
    assert len(cues) == 1
    c = cues[0]
    # 65 min elapsed at a 30-min interval → bucket 2, the 60-min mark.
    assert c.dedup_key == "elapsed_callout:1:2"
    assert c.ref == {"session_id": 1, "elapsed_minutes": 60}
    assert c.urgency == "nudge" and c.intervention == "elapsed_time_callouts"
    assert "60 min" in c.text and "the refactor" in c.text


def test_elapsed_callouts_silent_before_first_mark(store):
    store.set_state("elapsed_callout_minutes", "30")
    store.start_focus_session("just started", started_at=_started_ago(10))  # < 30
    assert TimeBlindnessModule().evaluate(store, CoachContext(now=utcnow())) == []


def test_elapsed_callout_profile_line_when_enabled(store):
    store.set_state("elapsed_callout_minutes", "30")
    section = TimeBlindnessModule().profile_section(store) or ""
    assert "every **30 min**" in section


def test_departure_cue_fires_when_leave_by_is_due(store):
    """A commitment whose leave-by has come due emits a departure_buffer cue on the
    tick, so `coach --deliver` sends it without the n8n departure poll."""
    from datetime import timedelta

    # Starts in 5 min with a 10-min lead → leave-by is ~5 min past → "go".
    soon = (utcnow() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    store.upsert_commitment(title="Dentist", start_at=soon, lead_minutes=10.0, source="manual")
    cues = TimeBlindnessModule().evaluate(store, CoachContext(now=utcnow(), display_name="Tom"))
    dep = [c for c in cues if c.context_key == "departure"]
    assert len(dep) == 1
    c = dep[0]
    assert c.intervention == "departure_buffer"
    assert c.urgency == "critical"  # past the leave-by → "go" → bypasses quiet hours
    assert c.dedup_key.endswith(":go") and c.dedup_key.startswith("departure:")
    assert c.ref.get("commitment_id")
    assert "Dentist" in c.text


def test_no_departure_cue_without_an_upcoming_commitment(store):
    """No commitments → no departure cue (evaluate stays silent)."""
    cues = TimeBlindnessModule().evaluate(store, CoachContext(now=utcnow()))
    assert not [c for c in cues if c.context_key == "departure"]


def _plan(cid, start_at, leave_by, *, mode="travel", title="Thing", location=None):
    """A minimal DeparturePlan for morning-prep tests (only the read fields matter)."""
    from prefrontal.departure import DeparturePlan

    return DeparturePlan(
        commitment={"id": cid, "title": title, "start_at": start_at, "location": location},
        leave_by=leave_by,
        minutes_until_leave=0.0,
        travel_minutes=None,
        basis="lead",
        level="none",
        mode=mode,
    )


def test_early_morning_plans_keys_off_leave_by_and_tomorrow():
    """A 9am appointment 45 min away (leave-by ~8:00) counts as an early start
    even though it *starts* after the 08:30 cutoff; a late-morning one doesn't;
    and only tomorrow's commitments are in scope. Sorted by leave-by."""
    from prefrontal.modules.time_blindness import early_morning_plans

    now = datetime(2026, 7, 6, 21, 30)  # the evening before, 21:30 UTC
    plans = [
        _plan(1, "2026-07-07 09:00:00", "2026-07-07 08:00:00"),  # leave-by early → in
        _plan(2, "2026-07-07 10:00:00", "2026-07-07 09:45:00"),  # leave-by late  → out
        _plan(3, "2026-07-06 22:30:00", "2026-07-06 22:00:00"),  # today, not tomorrow → out
        _plan(4, "2026-07-07 08:15:00", "2026-07-07 08:10:00", mode="attend"),  # early → in
    ]
    early = early_morning_plans(plans, now, "UTC", 8, 30)
    assert [p.commitment["id"] for p in early] == [1, 4]  # by leave-by ascending


def test_morning_prep_message_shape():
    """The heads-up names the commitment, its start, the be-out-by time, and the
    alarm hint — and drops the leave line for an attend-from-here (no travel) item."""
    from prefrontal.modules.time_blindness import morning_prep_message

    msg = morning_prep_message(
        "Dentist", "08:30", where=" (Downtown)", leave_hhmm="07:45", name="Tom"
    )
    assert "Dentist" in msg and "Downtown" in msg
    assert "08:30" in msg and "07:45" in msg and "alarm" in msg.lower()
    solo = morning_prep_message("Standup", "08:00")  # no leave-by
    assert "out the door" not in solo and "08:00" in solo


def test_morning_prep_cue_fires_in_the_evening_for_an_early_start(store):
    """With the evening gate open, a commitment tomorrow whose leave-by beats the
    threshold emits one morning_prep cue reminding the user to set an alarm."""
    from datetime import timedelta

    tomorrow = utcnow() + timedelta(days=1)
    start = tomorrow.replace(hour=8, minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    store.upsert_commitment(title="Flight", start_at=start, lead_minutes=45.0, source="manual")
    store.set_state("morning_prep_hour", "0")  # open the evening window for the test
    cues = TimeBlindnessModule().evaluate(store, CoachContext(now=utcnow(), display_name="Tom"))
    prep = [c for c in cues if c.context_key == "morning_prep"]
    assert len(prep) == 1
    c = prep[0]
    assert c.intervention == "morning_prep" and c.urgency == "nudge"
    assert c.dedup_key.startswith("morning_prep:")
    assert c.ref.get("commitment_id")
    assert "Flight" in c.text and "alarm" in c.text.lower()


def test_morning_prep_gate_only_fires_in_the_evening(store):
    """The heads-up waits for the evening send window — silent in the morning."""
    plans = [_plan(1, "2026-07-07 08:00:00", "2026-07-07 07:15:00", title="Flight")]
    mod = TimeBlindnessModule()
    evening = CoachContext(now=datetime(2026, 7, 6, 21, 30), timezone="UTC", display_name="Tom")
    morning = CoachContext(now=datetime(2026, 7, 6, 9, 0), timezone="UTC")
    assert len(mod._morning_prep_cues(store, plans, evening)) == 1
    assert mod._morning_prep_cues(store, plans, morning) == []


def test_morning_prep_cue_carries_alarm_button_payload(store):
    """The cue's ref carries a suggested wake time (leave-by minus the morning
    routine) and the Shortcut name, so delivery can attach a one-tap Set-alarm button."""
    plans = [_plan(1, "2026-07-07 08:00:00", "2026-07-07 07:15:00", title="Flight")]
    ctx = CoachContext(now=datetime(2026, 7, 6, 21, 30), timezone="UTC", display_name="Tom")
    cues = TimeBlindnessModule()._morning_prep_cues(store, plans, ctx)
    assert len(cues) == 1
    ref = cues[0].ref
    assert ref["alarm_at"] == "06:15"  # 07:15 leave-by − 60 min default routine
    assert ref["alarm_shortcut"] == "Set Alarm"


def test_morning_prep_alarm_backs_off_start_for_attend_mode(store):
    """Attend-from-here has no travel, so the wake time backs off the start itself."""
    plans = [_plan(1, "2026-07-07 08:15:00", "2026-07-07 08:10:00", mode="attend")]
    store.set_state("morning_routine_minutes", "45")
    ctx = CoachContext(now=datetime(2026, 7, 6, 21, 30), timezone="UTC")
    cues = TimeBlindnessModule()._morning_prep_cues(store, plans, ctx)
    assert cues[0].ref["alarm_at"] == "07:30"  # 08:15 start − 45 min


def test_morning_prep_fires_within_responsive_hours_when_prep_hour_is_quiet(store):
    """Regression: the "set an alarm" nudge is a non-critical cue, so quiet hours
    hold it whenever it's emitted outside the responsive window. A user who winds
    down early (responsive hours ending at 21:00, the default prep hour) would
    otherwise have an empty send window — the cue regenerated every evening but
    suppressed on every tick, so the alarm nudge silently never arrives. The prep
    hour is clamped into the last responsive hour so there's always a live window."""
    plans = [_plan(1, "2026-07-07 07:30:00", "2026-07-07 07:20:00", title="Tom Workout")]
    mod = TimeBlindnessModule()
    # Responsive hours end at 21:00, so the default 21:00 prep hour is already quiet.
    # The clamp moves generation to 20:00, where delivery isn't yet suppressed.
    last_hr = CoachContext(
        now=datetime(2026, 7, 6, 20, 30), timezone="UTC", responsive_end=21
    )
    too_early = CoachContext(
        now=datetime(2026, 7, 6, 19, 30), timezone="UTC", responsive_end=21
    )
    assert len(mod._morning_prep_cues(store, plans, last_hr)) == 1  # 20:xx: fires
    assert mod._morning_prep_cues(store, plans, too_early) == []  # 19:xx: not yet


def test_morning_prep_respects_configured_hour_within_responsive_window(store):
    """When the prep hour sits inside the responsive window (the common case), it is
    left exactly as configured — the clamp only rescues an otherwise-empty window."""
    plans = [_plan(1, "2026-07-07 07:30:00", "2026-07-07 07:20:00", title="Tom Workout")]
    mod = TimeBlindnessModule()
    # prep 21, responsive end 22 (defaults): the window [21, 22) is live, no clamp.
    before = CoachContext(now=datetime(2026, 7, 6, 20, 30), timezone="UTC", responsive_end=22)
    at = CoachContext(now=datetime(2026, 7, 6, 21, 30), timezone="UTC", responsive_end=22)
    assert mod._morning_prep_cues(store, plans, before) == []  # 20:xx: before prep hour
    assert len(mod._morning_prep_cues(store, plans, at)) == 1  # 21:xx: fires


def test_repeat_stalled_tasks_flags_repeat_misses_but_not_resolved():
    """Two+ misses on a task flag it; a task later completed drops off."""
    episodes = [
        {"id": 1, "context": "todo dropped: Call the dentist", "outcome": "miss"},
        {"id": 2, "context": "todo dropped: Call the dentist", "outcome": "miss"},
        {"id": 3, "context": "todo dropped: File taxes", "outcome": "miss"},
        {"id": 4, "context": "todo dropped: File taxes", "outcome": "miss"},
        {"id": 5, "context": "todo done: File taxes", "outcome": "success"},
        {"id": 6, "context": "outing: coffee", "outcome": "miss"},  # not a todo
    ]
    stuck = repeat_stalled_tasks(episodes)
    titles = [s["title"] for s in stuck]
    assert titles == ["Call the dentist"]  # File taxes resolved; outing ignored
    assert stuck[0]["misses"] == 2 and stuck[0]["attempts"] == 2


def test_repeat_stalled_tasks_respects_min_misses():
    """A single miss isn't yet a body-double signal."""
    episodes = [{"id": 1, "context": "todo dropped: Email Sam", "outcome": "miss"}]
    assert repeat_stalled_tasks(episodes) == []
    assert repeat_stalled_tasks(episodes, min_misses=1)[0]["title"] == "Email Sam"


def test_task_paralysis_profile_names_stuck_tasks(store):
    """The profile section names a task the user keeps bailing on."""
    for _ in range(2):
        store.log_episode("task", outcome="miss", context="todo dropped: Renew passport")
    get("task_paralysis").seed(store)
    section = get("task_paralysis").profile_section(store)
    assert section is not None
    assert "Keeps bailing on" in section
    assert "Renew passport" in section
