"""Tests for the morning briefing.

Covers the structured build (today's commitments, conflicts, weekly slips,
coaching note), the deterministic rendering (short vs long), the LLM-with-fallback
path, and the /briefing endpoint.
"""

from __future__ import annotations

from datetime import timedelta

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
