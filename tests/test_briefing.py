"""Tests for the morning briefing.

Covers the structured build (today's commitments, conflicts, weekly slips,
coaching note), the deterministic rendering (short vs long), the LLM-with-fallback
path, and the /briefing endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.briefing import (
    build_briefing,
    render_briefing,
    summarize_briefing,
)
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "brief-secret"


def _at(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def test_build_briefing_scopes_to_local_day(store, monkeypatch):
    """"Today" follows the user's *local* day, not the UTC day.

    At 9pm Eastern the UTC day has already rolled over. A UTC-day briefing would
    drop this-morning's commitment and pull in tomorrow-morning's; scoping to the
    local day keeps today's and excludes tomorrow's.
    """
    from prefrontal import briefing as briefing_mod

    monkeypatch.setattr(
        briefing_mod, "get_settings",
        lambda: Settings(webhook_secret=SECRET, timezone="America/New_York"),
    )
    # now = 2026-07-07 01:00 UTC = 2026-07-06 21:00 EDT → local day is the 6th.
    now = datetime(2026, 7, 7, 1, 0, 0)
    store.upsert_commitment(  # 10:00 EDT on the 6th — today (local)
        title="This morning", start_at="2026-07-06 14:00:00", external_id="x:today",
    )
    store.upsert_commitment(  # 09:00 EDT on the 7th — tomorrow (local)
        title="Tomorrow AM", start_at="2026-07-07 13:00:00", external_id="x:tmrw",
    )
    titles = {c["title"] for c in build_briefing(store, now=now).today}
    assert "This morning" in titles
    assert "Tomorrow AM" not in titles


def test_briefing_renders_times_in_local_zone(store, monkeypatch):
    """Commitment/leave-by/spare times read in the local zone, not raw UTC.

    Stored times are naive UTC; an Eastern user's 10:00 EDT commitment is stored
    as 14:00 UTC. The digest must show "10:00", not "14:00" (the old bug: it
    sliced the UTC string and printed times 4h ahead).
    """
    from prefrontal import briefing as briefing_mod

    monkeypatch.setattr(
        briefing_mod, "get_settings",
        lambda: Settings(webhook_secret=SECRET, timezone="America/New_York"),
    )
    # now = 2026-07-06 13:00 UTC = 09:00 EDT — morning, local day is the 6th.
    now = datetime(2026, 7, 6, 13, 0, 0)
    store.upsert_commitment(  # 14:00 UTC == 10:00 EDT
        title="Dentist", start_at="2026-07-06 14:00:00",
        end_at="2026-07-06 14:30:00", external_id="personal:1", lead_minutes=15.0,
    )
    text = render_briefing(build_briefing(store, now=now))
    assert "- 10:00 — Dentist" in text  # commitment line in local time
    assert "14:00" not in text  # the UTC wall clock never leaks through
    assert "Leave by 09:45 for Dentist" in text  # leave-by (start − 15 min) in local time


def test_build_briefing_collects_today_conflicts_slips(store):
    """The structured briefing gathers today's events, conflicts, and slips."""
    now = utcnow()
    today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # Two overlapping commitments today.
    store.upsert_commitment(
        title="Dentist", start_at=_at(today_noon),
        end_at=_at(today_noon + timedelta(minutes=45)),
        external_id="personal:1", hardness="hard",
    )
    store.upsert_commitment(
        title="Team sync", start_at=_at(today_noon + timedelta(minutes=30)),
        end_at=_at(today_noon + timedelta(minutes=60)), external_id="work:1",
    )
    # A commitment two days out (not "today").
    store.upsert_commitment(
        title="Future", start_at=_at(now + timedelta(days=2)), external_id="work:2"
    )
    # Recent slips.
    store.log_episode("reminder", outcome="miss")
    store.log_episode("departure", outcome="miss")
    store.log_episode("task", outcome="success")  # not a slip

    b = build_briefing(store, now=now)
    assert {c["title"] for c in b.today} == {"Dentist", "Team sync"}
    assert len(b.conflicts) == 1
    assert b.slips == {"reminder": 1, "departure": 1}
    assert b.coaching["time_estimation_bias"] == "1.4"


def test_briefing_flags_fragile_stretch(store):
    """Tight back-to-backs that hold on paper are previewed under the time bias."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    # Two 30-min meetings 35 min apart — 5 min buffer, fine as scheduled but
    # doomed once the default 1.4× overrun eats the buffer.
    store.upsert_commitment(
        title="Design review", start_at=_at(now + timedelta(minutes=60)),
        end_at=_at(now + timedelta(minutes=90)), external_id="work:d1",
    )
    store.upsert_commitment(
        title="1:1", start_at=_at(now + timedelta(minutes=95)),
        end_at=_at(now + timedelta(minutes=125)), external_id="work:d2",
    )
    b = build_briefing(store, now=now)
    assert [f["title"] for f in b.fragile] == ["1:1"]
    assert b.fragile[0]["caused_by"] == "Design review"
    text = render_briefing(b)
    assert "Tight stretch" in text
    assert "1:1" in text and "Design review" in text


def test_briefing_fragile_ignores_fyi(store):
    """An FYI event can't be toppled by your own overrun — it's not your time."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    # A real meeting, then a back-to-back FYI event that would "collide" under the
    # bias if it were yours. It isn't — you go nowhere for it, so it's never flagged.
    store.upsert_commitment(
        title="Design review", start_at=_at(now + timedelta(minutes=60)),
        end_at=_at(now + timedelta(minutes=90)), external_id="work:d1",
    )
    store.upsert_commitment(
        title="Partner's brow appt", start_at=_at(now + timedelta(minutes=95)),
        end_at=_at(now + timedelta(minutes=125)), external_id="personal:fyi",
        kind="fyi",
    )
    b = build_briefing(store, now=now)
    assert b.fragile == []
    assert "Tight stretch" not in render_briefing(b)


def test_briefing_spare_offers_alternatives(store):
    """A spare window offers a primary plus a couple of alternatives ('or: …')."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    for title in ("Tidy the desk", "Water the plants", "Sort the mail"):
        store.add_todo(title, estimate_minutes=10, priority=1)
    b = build_briefing(store, now=now)
    suggested = [s for s in b.spare if s["suggestion"]]
    assert suggested and suggested[0]["alternatives"]  # primary + >= 1 alternative
    assert "or:" in render_briefing(b)


def test_briefing_no_fragile_when_day_has_slack(store):
    """A spaced-out day surfaces no tight-stretch line."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    store.upsert_commitment(
        title="Morning", start_at=_at(now + timedelta(minutes=30)),
        end_at=_at(now + timedelta(minutes=60)), external_id="work:s1",
    )
    store.upsert_commitment(
        title="Afternoon", start_at=_at(now + timedelta(hours=6)),
        end_at=_at(now + timedelta(hours=6, minutes=30)), external_id="work:s2",
    )
    b = build_briefing(store, now=now)
    assert b.fragile == []
    assert "Tight stretch" not in render_briefing(b)


def test_briefing_surfaces_leave_by_for_travel_commitment(store):
    """A travel commitment still ahead today gets a leave-by line."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    store.upsert_commitment(
        title="Dentist", start_at=_at(now + timedelta(minutes=120)),
        end_at=_at(now + timedelta(minutes=150)), external_id="personal:1",
        lead_minutes=15.0,  # travel+prep buffer -> leave 15 min before start
    )
    b = build_briefing(store, now=now)
    assert [d["title"] for d in b.departures] == ["Dentist"]
    # leave-by is start − lead (basis "lead", no coords), i.e. 15 min earlier.
    leave = b.departures[0]["leave_by"]
    assert leave[11:16] == (now + timedelta(minutes=105)).strftime("%H:%M")
    text = render_briefing(b)
    assert "🚶 Leave by " in text
    assert "for Dentist" in text


def test_briefing_leave_by_skips_attend_mode_and_past(store):
    """Attend-from-desk (work feed) and already-started commitments get no leave-by."""
    now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    # A work-calendar meeting today, still ahead → attend mode (no travel).
    store.upsert_commitment(
        title="Standup", start_at=_at(now + timedelta(minutes=90)),
        end_at=_at(now + timedelta(minutes=120)), external_id="work:1",
    )
    # A travel commitment that already started → past, excluded.
    store.upsert_commitment(
        title="Gym", start_at=_at(now - timedelta(minutes=30)),
        end_at=_at(now + timedelta(minutes=30)), external_id="personal:1",
    )
    b = build_briefing(store, now=now)
    assert b.departures == []
    assert "Leave by" not in render_briefing(b)


def test_briefing_leave_by_skips_fyi_and_placeholder(store):
    """FYI events and placeholder holds never get a leave-by (you're not going)."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    # An FYI event — where a partner will be. You attend nothing, go nowhere.
    store.upsert_commitment(
        title="Partner's brow appt", start_at=_at(now + timedelta(minutes=120)),
        external_id="personal:fyi", lead_minutes=15.0, kind="fyi",
    )
    # A placeholder hold — elastic time, nothing to leave by for.
    store.upsert_commitment(
        title="HOLD", start_at=_at(now + timedelta(minutes=180)),
        external_id="personal:hold", lead_minutes=15.0,
    )
    b = build_briefing(store, now=now)
    assert b.departures == []
    assert "Leave by" not in render_briefing(b)


def test_briefing_leave_by_off_when_time_blindness_disabled(store, monkeypatch):
    """With the Time Blindness module off, no leave-by is computed."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    store.upsert_commitment(
        title="Dentist", start_at=_at(now + timedelta(minutes=120)),
        external_id="personal:1", lead_minutes=15.0,
    )
    # Time Blindness owns departure timing; disable its gate.
    monkeypatch.setattr(
        "prefrontal.briefing.module_enabled",
        lambda key, *a, **k: key != "time_blindness",
    )
    b = build_briefing(store, now=now)
    assert b.departures == []


def test_render_short_vs_long(store):
    """Long format lists every commitment; short stays terse for big days."""
    now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    for i in range(8):
        store.upsert_commitment(
            title=f"Mtg {i}", start_at=_at(now + timedelta(hours=i + 1)),
            external_id=f"work:{i}",
        )
    store.set_state("preferred_briefing_format", "short", source="explicit")
    short = render_briefing(build_briefing(store, now=now))
    assert "8 commitment" in short
    assert "Mtg 0" not in short  # short omits the full list for a big day

    store.set_state("preferred_briefing_format", "long", source="explicit")
    long = render_briefing(build_briefing(store, now=now))
    assert "Mtg 0" in long and "Mtg 7" in long


def test_briefing_coaching_surfaces_user_name(store):
    """user_name is included in the briefing's coaching dict (for the family view)."""
    store.set_state("user_name", "Tom", source="explicit")
    assert build_briefing(store).coaching.get("user_name") == "Tom"


def test_render_empty_day(store):
    """An empty calendar reads cleanly."""
    text = render_briefing(build_briefing(store))
    assert "nothing on the calendar" in text


def test_render_includes_bias_reminder(store):
    """The coaching note surfaces the underestimate percentage."""
    text = render_briefing(build_briefing(store))
    assert "underestimate time by ~40%" in text


def test_briefing_surfaces_recent_triage_items(store):
    """Triage 'surface' items from the last day appear; drops and stale ones don't."""
    now = utcnow()

    def _at_ts(hours_ago):
        return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    store.log_triage(
        source="mail", title="Package delivered", kind="info", urgency="none",
        route="surface", reason="worth seeing once", confidence=0.4,
        decided_by="heuristic", received_at=_at_ts(2),
    )
    store.log_triage(  # > 24h → ages out
        source="mail", title="Old notice", kind="info", urgency="none",
        route="surface", reason="stale", confidence=0.4,
        decided_by="heuristic", received_at=_at_ts(30),
    )
    store.log_triage(  # dropped → never surfaced
        source="mail", title="Spammy promo", kind="noise", urgency="none",
        route="drop", reason="junk", confidence=0.9,
        decided_by="heuristic", received_at=_at_ts(1),
    )
    b = build_briefing(store, now=now)
    assert [s["title"] for s in b.surfaced] == ["Package delivered"]
    text = render_briefing(b)
    assert "Worth a look" in text and "Package delivered" in text
    assert "Old notice" not in text and "Spammy promo" not in text


class _FakeClient:
    def __init__(self, reply="", error=False, model="fake"):
        self.reply, self.error, self.model = reply, error, model

    def generate(self, prompt, *, system=None):
        if self.error:
            raise OllamaError("down")
        return self.reply


def test_briefing_feedback_tally_and_guidance(store):
    """👍/👎 votes accumulate and, past the margin, steer the LLM prompt."""
    from prefrontal.briefing import (
        learned_briefing_guidance,
        record_briefing_feedback,
    )

    assert learned_briefing_guidance(store) == ""  # no signal yet
    tally = record_briefing_feedback(store, helpful=False)
    assert tally == {"helpful": 0, "not_helpful": 1}
    assert learned_briefing_guidance(store) == ""  # one vote is below the margin
    record_briefing_feedback(store, helpful=False)
    assert "Tighten up" in learned_briefing_guidance(store)

    # Enough 👍 to flip the balance back the other way.
    for _ in range(4):
        record_briefing_feedback(store, helpful=True)
    assert "keep this shape" in learned_briefing_guidance(store)


class _CapturingClient:
    """A fake client that records the system prompt it was handed."""

    def __init__(self):
        self.system = None
        self.model = "fake"

    def generate(self, prompt, *, system=None):
        self.system = system
        return "Morning! Tight and focused today."


def test_summarize_briefing_folds_feedback_into_prompt(store):
    """A run of 👎 reaches the model as an appended 'tighten up' instruction."""
    from prefrontal.briefing import record_briefing_feedback

    for _ in range(2):
        record_briefing_feedback(store, helpful=False)
    client = _CapturingClient()
    summarize_briefing(store, client=client)
    assert "Tighten up" in client.system


def test_summarize_briefing_llm_and_fallback(store):
    """LLM prose when available; deterministic digest on failure."""
    ok = summarize_briefing(store, client=_FakeClient(reply="Morning! Light day ahead."))
    assert ok.source == "llm" and "Morning!" in ok.text

    fb = summarize_briefing(store, client=_FakeClient(error=True))
    assert fb.source == "heuristic" and "Morning briefing" in fb.text


def test_briefing_endpoint(store):
    """GET /briefing returns structured data plus rendered text, token-guarded."""
    now = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    store.upsert_commitment(
        title="Standup", start_at=_at(now + timedelta(hours=1)), external_id="work:1"
    )
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        assert c.get("/briefing").status_code == 401
        body = c.get("/briefing", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["today"][0]["title"] == "Standup"
    assert "Morning briefing" in body["text"]


# --- Encouragement in the briefing (spec §6.2) -------------------------------


def _morning(store):
    """Enable the encouragement layer and return a fixed 9am 'now'."""
    store.set_state("encouragement", "on", source="explicit")
    return utcnow().replace(hour=9, minute=0, second=0, microsecond=0)


def test_encouragement_off_keeps_bias_reminder(store):
    """Layer off (default): no note, the briefing keeps its time-bias reminder."""
    now = _morning(store)
    store.set_state("encouragement", "off", source="explicit")
    for i in range(3):
        store.upsert_commitment(
            title=f"Mtg {i}", start_at=_at(now + timedelta(hours=i + 1)),
            external_id=f"work:{i}", hardness="hard",
        )
    b = build_briefing(store, now=now)
    assert b.encouragement is None
    assert "underestimate time" in render_briefing(b)


def test_packed_day_encourages(store):
    """A packed day (≥3 hard) gets a 'you've got this' + space-it-out note."""
    now = _morning(store)
    for i in range(3):
        store.upsert_commitment(
            title=f"Mtg {i}", start_at=_at(now + timedelta(hours=i + 1)),
            external_id=f"work:{i}", hardness="hard",
        )
    b = build_briefing(store, now=now)
    assert b.encouragement and "you've got this" in b.encouragement.lower()
    text = render_briefing(b)
    assert b.encouragement in text
    assert "underestimate time" not in text  # note replaces the bias reminder


def test_recent_rough_stretch_encourages(store):
    """Misses across the week (not today) trip the recently-rough note."""
    now = _morning(store)
    # Three misses two days ago — inside the 7-day slip window but not "today",
    # so this is the *recent stretch* branch, not today's acute assessment.
    earlier = _at(now - timedelta(days=2))
    for _ in range(3):
        store.log_episode("reminder", outcome="miss", timestamp=earlier)
    b = build_briefing(store, now=now)
    assert b.encouragement and "space things out" in b.encouragement.lower()


def test_open_day_presents_choice_then_acts(store):
    """A wide-open day asks the relax/accomplish question, then honors the answer."""
    now = _morning(store)
    store.add_todo("Draft the report", priority=2, estimate_minutes=30)

    ask = build_briefing(store, now=now).encouragement
    assert ask and "relax or accomplish" in ask.lower()

    store.set_state("open_day_choice", "relax", source="user")
    relax = build_briefing(store, now=now).encouragement
    assert relax and "rest days" in relax.lower()
    assert "Draft the report" in relax  # one optional low-stakes item

    store.set_state("open_day_choice", "accomplish", source="user")
    plan = build_briefing(store, now=now).encouragement
    assert plan and "make-it-count" in plan.lower()
    assert "Draft the report" in plan  # a concrete light plan


def test_open_day_endpoint_choice_roundtrip(store):
    """GET exposes the choice; POST /briefing/open-day sets and clears it."""
    store.set_state("encouragement", "on", source="explicit")
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    hdr = {"X-Prefrontal-Token": SECRET}
    with TestClient(app) as c:
        assert c.get("/briefing", headers=hdr).json()["open_day_choice"] is None
        set_resp = c.post("/briefing/open-day", headers=hdr, json={"choice": "accomplish"})
        assert set_resp.json()["open_day_choice"] == "accomplish"
        assert c.get("/briefing", headers=hdr).json()["open_day_choice"] == "accomplish"
        clear = c.post("/briefing/open-day", headers=hdr, json={"choice": "ask"})
        assert clear.json()["open_day_choice"] is None


# -- switch-rate feedback (Impulsivity) --------------------------------------


def test_briefing_switch_feedback_from_recent_focus(store):
    """A closed focus session with switch-impulses surfaces the feedback line."""
    now = utcnow()
    sid = store.start_focus_session("deep work", planned_minutes=60)
    store.record_switch_impulse(sid)
    store.record_switch_impulse(sid)
    store.mark_switch_deferred(sid)
    store.close_focus_session(sid, "ended")

    b = build_briefing(store, now=now)
    assert b.switch_feedback == "2 switch-impulses, 1 deferred"
    assert "Focus switches" in render_briefing(b)


def test_briefing_no_switch_line_when_no_impulses(store):
    """A clean focus block (no impulses) adds no switch line."""
    sid = store.start_focus_session("deep work")
    store.close_focus_session(sid, "ended")

    b = build_briefing(store, now=utcnow())
    assert b.switch_feedback is None
    assert "Focus switches" not in render_briefing(b)
