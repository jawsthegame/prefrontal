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
from prefrontal.modules.task_paralysis import (
    DEFAULT_BODY_DOUBLE_WINDOW_MINUTES,
    repeat_stalled_tasks,
    start_body_double,
)
from prefrontal.modules.time_blindness import (
    DEFAULT_MORNING_ROUTINE_MINUTES,
    TimeBlindnessModule,
    adapt_morning_routine,
)
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

    # The engine collects the piercing keys from each module's pierces_protection
    # flag once per tick; self-care and hyperfocus declare it. Here we pass them
    # directly since the test drives suppressed() rather than a full tick.
    ctx = CoachContext(
        now=utcnow(), responsive_start=0, responsive_end=0, focus_protected=True,
        pierce_keys=frozenset({"self_care", "hyperfocus"}),
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


def test_start_body_double_opens_a_real_focus_session(store):
    """A body-double is a short aligned focus session on the stuck todo."""
    tid = store.add_todo("Renew passport", estimate_minutes=90, priority=3)
    result = start_body_double(store, todo=store.get_todo(tid), window_minutes=15)
    assert result["todo_id"] == tid
    assert result["planned_minutes"] == 15.0
    # A real, active focus session now exists, linked to the todo and aligned.
    active = store.active_focus_sessions()
    assert len(active) == 1
    session = active[0]
    assert session["id"] == result["session_id"]
    assert session["todo_id"] == tid
    assert session["aligned"] == 1
    assert session["planned_minutes"] == 15.0
    assert "Renew passport" in result["message"]


def test_start_body_double_uses_stored_first_step(store):
    """The message leads with the decomposition's first step when there is one."""
    tid = store.add_todo("File taxes", estimate_minutes=120)
    store.set_decomposition(
        tid,
        first_step="Open the tax portal",
        first_step_minutes=5.0,
        steps=["gather docs"],
        source="llm",
    )
    result = start_body_double(store, todo=store.get_todo(tid))
    assert "Open the tax portal" in result["message"]
    assert result["planned_minutes"] == float(DEFAULT_BODY_DOUBLE_WINDOW_MINUTES)


def test_start_body_double_solo_has_no_partner_invite(store):
    """Outside a household there's no co-parent to invite — just a timer."""
    tid = store.add_todo("Clean the garage")
    result = start_body_double(store, todo=store.get_todo(tid))
    assert "Ask" not in result["message"]


def test_task_paralysis_profile_names_stuck_tasks(store):
    """The profile section names a task the user keeps bailing on."""
    for _ in range(2):
        store.log_episode("task", outcome="miss", context="todo dropped: Renew passport")
    get("task_paralysis").seed(store)
    section = get("task_paralysis").profile_section(store)
    assert section is not None
    assert "Keeps bailing on" in section
    assert "Renew passport" in section


# -- learned morning-routine lead --------------------------------------------


def _log_departure(store, *, notes, outcome):
    """Log a ``departure`` episode with a note in record_departure_outcome's format."""
    store.log_episode(
        "departure",
        predicted_value=15.0,
        actual_value=None,
        acknowledged=True,
        context="auto departure: School run",
        outcome=outcome,
        notes=notes,
    )


def test_morning_routine_widens_when_chronically_late(store):
    """Repeatedly leaving late on early starts grows the suggested wake lead."""
    get("time_blindness").seed(store)
    for _ in range(5):
        _log_departure(store, notes="left ~15 min late (leave-by 07:30)", outcome="miss")
    result = adapt_morning_routine(store)
    assert result["changed"] is True
    # +15 min mean lateness pushes the 60-min default toward ~75.
    assert result["routine"] == 75
    assert int(store.get_float("morning_routine_minutes", 0)) == 75


def test_morning_routine_eases_back_when_leaving_early(store):
    """Consistently leaving with time to spare trims the lead so you sleep more."""
    get("time_blindness").seed(store)
    for _ in range(5):
        _log_departure(store, notes="left on time (~20 min to spare, leave-by 07:30)",
                       outcome="success")
    result = adapt_morning_routine(store)
    assert result["changed"] is True
    assert result["routine"] == 40  # 60 − 20


def test_morning_routine_ignores_midday_departures(store):
    """Only early-start departures (leave-by before the threshold) count."""
    get("time_blindness").seed(store)
    for _ in range(5):
        _log_departure(store, notes="left ~15 min late (leave-by 14:00)", outcome="miss")
    result = adapt_morning_routine(store)
    assert result["changed"] is False
    assert result["samples"] == 0


def test_morning_routine_holds_within_deadband(store):
    """On-time early starts leave the lead alone (no churn)."""
    get("time_blindness").seed(store)
    for _ in range(5):
        _log_departure(store, notes="left right on time (leave-by 07:30)", outcome="success")
    result = adapt_morning_routine(store)
    assert result["changed"] is False
    assert result["routine"] == DEFAULT_MORNING_ROUTINE_MINUTES


def test_morning_routine_respects_explicit_value(store):
    """A lead the user set by hand is never overridden by the learner."""
    get("time_blindness").seed(store)
    store.set_state("morning_routine_minutes", "45", source="explicit")
    for _ in range(5):
        _log_departure(store, notes="left ~15 min late (leave-by 07:30)", outcome="miss")
    result = adapt_morning_routine(store)
    assert result["changed"] is False
    assert int(store.get_float("morning_routine_minutes", 0)) == 45


def test_morning_routine_needs_enough_samples(store):
    """Below the sample floor it reports but doesn't move."""
    get("time_blindness").seed(store)
    _log_departure(store, notes="left ~15 min late (leave-by 07:30)", outcome="miss")
    result = adapt_morning_routine(store)
    assert result["changed"] is False
    assert result["samples"] == 1


def test_morning_routine_parses_real_departure_notes(store):
    """The learner reads the exact note format record_departure_outcome emits."""
    from datetime import timedelta

    from prefrontal.departure import DeparturePlan, record_departure_outcome

    get("time_blindness").seed(store)
    now = utcnow()
    # A 07:15 leave-by, left 15 min late — an early start that ran late.
    start = now + timedelta(minutes=45)
    leave_by = now + timedelta(minutes=15)
    plan = DeparturePlan(
        commitment={
            "id": 1,
            "title": "School run",
            "start_at": start.strftime("%Y-%m-%d %H:%M:%S"),
        },
        leave_by=leave_by.strftime("%Y-%m-%d %H:%M:%S"),
        minutes_until_leave=15.0,
        travel_minutes=10.0,
        basis="distance",
        level="soon",
    )
    departed = leave_by + timedelta(minutes=15)
    # Threshold high enough that this leave-by qualifies as an early start.
    store.set_state("early_start_threshold", "23:59", source="explicit")
    for _ in range(5):
        record_departure_outcome(store, plan, departed, tz="UTC")
    result = adapt_morning_routine(store)
    assert result["changed"] is True
    assert result["routine"] > DEFAULT_MORNING_ROUTINE_MINUTES


# --- Open Window (calendar-gap-aware proactive nudge, M3) --------------------
#
# The module gates on work_window_now(ctx.now, tz), so every test pins ``now`` to a
# fixed midday instant rather than the wall clock — otherwise a suite run at 3am
# would flake on the waking-hours check.

_GAP_NOON = datetime(2026, 7, 2, 12, 0, 0)  # inside the default 06:00–22:00 waking band (UTC)


def _gap_ctx(now=_GAP_NOON):
    """A coaching context pinned to midday UTC, so the waking-hours bound is stable."""
    return CoachContext(now=now, timezone="UTC")


def _avoided_fitting_todo(
    store, *, title="File the reimbursement form", estimate_minutes=20.0,
    priority=2, days_open=5.0, now=_GAP_NOON,
):
    """An open todo old + important enough to read as avoided, sized to fit a gap."""
    tid = store.add_todo(title, estimate_minutes=estimate_minutes, priority=priority)
    created = (now - timedelta(days=days_open)).strftime("%Y-%m-%d %H:%M:%S")
    store.conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (created, tid))
    store.conn.commit()
    return tid


def _commitment_in(store, minutes_ahead, *, minutes_long=30.0, title="Team sync", now=_GAP_NOON):
    """An active commitment starting ``minutes_ahead`` of the pinned now."""
    start = (now + timedelta(minutes=minutes_ahead)).strftime("%Y-%m-%d %H:%M:%S")
    end = (now + timedelta(minutes=minutes_ahead + minutes_long)).strftime("%Y-%m-%d %H:%M:%S")
    store.upsert_commitment(title=title, start_at=start, end_at=end, source="manual")


def test_open_window_offers_avoided_todo_in_a_real_gap(store):
    """A genuine free window + an avoided todo that fits → one open_window nudge."""
    tid = _avoided_fitting_todo(store)
    _commitment_in(store, 45)  # 45 min free before the next thing
    cues = get("open_window").evaluate(store, _gap_ctx())
    assert len(cues) == 1
    cue = cues[0]
    assert cue.module == "open_window"
    assert cue.intervention == "gap_offer"
    assert cue.urgency == "nudge"  # never critical
    assert cue.context_key == "open_window"
    assert cue.dedup_key == f"open_window:{tid}"
    assert cue.ref["todo_id"] == tid
    assert cue.ref["gap_minutes"] == 45
    assert "File the reimbursement form" in cue.text
    assert "45 min" in cue.text


def test_open_window_silent_when_busy_now(store):
    """A commitment in progress (available_now == 0) → no offer."""
    _avoided_fitting_todo(store)
    _commitment_in(store, -10, minutes_long=60)  # started 10 min ago, still running
    assert get("open_window").evaluate(store, _gap_ctx()) == []


def test_open_window_silent_when_gap_below_minimum(store):
    """A gap shorter than coach_gap_min_minutes (default 15) → no offer."""
    _avoided_fitting_todo(store)
    _commitment_in(store, 10)  # only 10 min free
    assert get("open_window").evaluate(store, _gap_ctx()) == []


def test_open_window_silent_without_an_avoided_fitting_todo(store):
    """A real gap but only a fresh (not-yet-avoided) todo → nothing worth pushing.

    ``pick_now`` would return it as a plain ``"fits"`` best-guess; the proactive push
    fires only for a genuinely *avoided* pick, so this stays quiet.
    """
    store.add_todo("Skim the newsletter", estimate_minutes=10.0, priority=1)
    _commitment_in(store, 60)
    assert get("open_window").evaluate(store, _gap_ctx()) == []


def test_open_window_silent_outside_waking_hours(store):
    """Outside the waking band work_window_now says "not within" → no offer."""
    night = datetime(2026, 7, 2, 3, 0, 0)  # 03:00 UTC, inside the default off-zone
    _avoided_fitting_todo(store, now=night)
    _commitment_in(store, 45, now=night)
    assert get("open_window").evaluate(store, _gap_ctx(night)) == []


def test_open_window_silent_when_todo_window_excludes_now(store):
    """A todo whose own suggestion window excludes now is filtered out (todo_allowed_at)."""
    tid = _avoided_fitting_todo(store)
    # Pin the todo to an early-morning-only window; at midday it's not suggestible.
    store.set_todo_window(tid, "06:00-09:00")
    _commitment_in(store, 45)
    assert get("open_window").evaluate(store, _gap_ctx()) == []


def test_open_window_claims_todo_so_task_paralysis_stands_down(store):
    """Option A: when open_window offers a todo into a gap, task_paralysis must not
    also nudge that same todo this tick — the gap-anchored cue takes precedence."""
    tid = _avoided_fitting_todo(store)
    _commitment_in(store, 45)
    ctx = _gap_ctx()

    # Sanity: with no claim, task_paralysis would fire for that very avoided todo.
    solo = get("task_paralysis").evaluate(store, _gap_ctx())
    assert [c.ref["todo_id"] for c in solo] == [tid]

    # before_collect runs before any evaluate in a real tick and fills the shared
    # claim set, so the stand-down is order-independent.
    get("open_window").before_collect(store, ctx)
    assert tid in ctx.claimed_todo_ids

    assert [c.ref["todo_id"] for c in get("open_window").evaluate(store, ctx)] == [tid]
    # task_paralysis now skips the claimed todo — and it was the only avoided one, so
    # it stands down entirely.
    assert get("task_paralysis").evaluate(store, ctx) == []


def test_open_window_suppresses_identical_reoffer(store):
    """After an offer fires, the same (todo, next commitment) pitch is held next tick,
    so a long free afternoon doesn't re-fire every tick (beyond the debounce)."""
    from prefrontal.coaching import Decision

    _avoided_fitting_todo(store)
    _commitment_in(store, 45)
    ctx = _gap_ctx()
    mod = get("open_window")
    cues = mod.evaluate(store, ctx)
    assert len(cues) == 1
    # Simulate the engine committing the fire → the after_fire marker write.
    mod.after_fire(store, [Decision(cue=cues[0], channel="push", text=cues[0].text)], ctx)
    # Identical situation on the next tick → held.
    assert mod.evaluate(store, ctx) == []


def test_open_window_seeds_its_gap_minimum(store):
    """Enabling the module seeds the coach_gap_min_minutes tunable (non-clobbering)."""
    get("open_window").seed(store)
    assert store.get_state("coach_gap_min_minutes") == "15"
