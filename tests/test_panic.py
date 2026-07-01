"""Tests for panic mode.

Covers the structured triage (bucketing commitments / todos / mail into
late / soon / piling-up), the single first-step selection, the deterministic
rendering (including the all-clear and no-hard-clock paths), the LLM-with-fallback
pass, and the /panic endpoint.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.panic import build_panic, render_panic, summarize_panic
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "panic-secret"


def _at(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def noon():
    """A fixed midday 'now' so windowing never depends on wall-clock time."""
    return utcnow().replace(hour=12, minute=0, second=0, microsecond=0)


def test_late_commitment_becomes_a_fire_and_drives_the_first_step(store, noon):
    """A commitment whose safe-departure has passed is 'late' and leads the plan."""
    store.upsert_commitment(
        title="Dentist",
        start_at=_at(noon + timedelta(minutes=30)),
        lead_minutes=60,  # should have left at 11:30, it's noon
        external_id="personal:1",
        hardness="hard",
    )
    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == ["Leave for Dentist"]
    assert plan.late[0].source == "Personal"  # calendar label is title-cased
    assert plan.counts["pressing"] == 1
    # The one thing points at getting out the door.
    assert plan.first_step_for == "Leave for Dentist"
    assert "door" in plan.first_step.lower()


def test_started_hard_commitment_is_late_soft_ongoing_is_ignored(store, noon):
    """A hard meeting already underway is a fire; a soft block you're in is not."""
    store.upsert_commitment(
        title="Standup", start_at=_at(noon - timedelta(minutes=15)),
        end_at=_at(noon + timedelta(minutes=15)), external_id="work:1", hardness="hard",
    )
    store.upsert_commitment(
        title="Focus block", start_at=_at(noon - timedelta(minutes=15)),
        end_at=_at(noon + timedelta(minutes=15)), external_id="work:2", hardness="soft",
    )
    plan = build_panic(store, now=noon)
    titles = [p.title for p in plan.late]
    assert "Get to Standup" in titles
    assert all("Focus block" not in t for t in titles)
    assert "started 15 min ago" in plan.late[0].when


def test_soon_bucket_holds_upcoming_departures(store, noon):
    """A commitment inside the soon-window (but not yet late) lands in 'soon'."""
    store.upsert_commitment(
        title="Lunch with Sam", start_at=_at(noon + timedelta(minutes=50)),
        lead_minutes=10, external_id="personal:1",
    )
    plan = build_panic(store, now=noon)
    assert not plan.late
    assert [p.title for p in plan.soon] == ["Leave for Lunch with Sam"]
    assert plan.soon[0].when.startswith("leave in")


def test_ended_and_fyi_commitments_are_excluded(store, noon):
    """Finished events and FYI (someone-else) events never appear."""
    store.upsert_commitment(
        title="Over already", start_at=_at(noon - timedelta(hours=2)),
        end_at=_at(noon - timedelta(hours=1)), external_id="work:1", hardness="hard",
    )
    store.upsert_commitment(
        title="Kid's recital", start_at=_at(noon + timedelta(minutes=30)),
        lead_minutes=60, external_id="family:1", hardness="hard", kind="fyi",
    )
    plan = build_panic(store, now=noon)
    assert plan.late == [] and plan.soon == []
    assert plan.counts["pressing"] == 0


def test_todo_deadlines_bucket_and_urgent_flag(store, noon):
    """Overdue → late, due-today → soon, priority-3 no-deadline → soon."""
    yesterday = (noon - timedelta(days=2)).strftime("%Y-%m-%d")  # date-only
    today = noon.strftime("%Y-%m-%d")
    store.add_todo("File the taxes", deadline=yesterday, priority=2)
    store.add_todo("Submit expense report", deadline=today, priority=1)
    store.add_todo("Fix the leak", priority=3)  # urgent, no deadline

    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == ["File the taxes"]
    assert plan.late[0].when.endswith("overdue")
    soon_titles = {p.title for p in plan.soon}
    assert soon_titles == {"Submit expense report", "Fix the leak"}


def test_date_only_deadline_today_is_not_overdue(store, noon):
    """A todo due 'today' (date-only) is soon, not late — end-of-day, not midnight."""
    plan = build_panic(store, now=noon)
    store.add_todo("Renew passport", deadline=noon.strftime("%Y-%m-%d"))
    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == []
    assert [p.title for p in plan.soon] == ["Renew passport"]


def test_mail_urgency_splits_across_soon_and_piling_up(store, noon):
    """Urgent action-mail is 'soon'; high is 'piling_up'; each tagged by inbox."""
    store.record_mail(
        account="work", message_id="m1", sender_name="Boss", subject="Contract???",
        needs_action=True, urgency="urgent",
    )
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form due",
        needs_action=True, urgency="high",
    )
    plan = build_panic(store, now=noon)
    assert any(p.kind == "mail" and p.source == "work" for p in plan.soon)
    assert any(p.kind == "mail" and p.source == "home" for p in plan.piling_up)


def test_first_step_prefers_a_clocked_item_over_a_smoulder(store, noon):
    """The forced first step comes from late/soon, never from piling-up alone."""
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form",
        needs_action=True, urgency="high",  # piling_up only
    )
    store.add_todo("Reply to landlord", deadline=noon.strftime("%Y-%m-%d"))  # soon
    plan = build_panic(store, now=noon)
    assert plan.first_step_for == "Reply to landlord"
    assert plan.first_step  # a concrete, non-empty action


def test_render_all_clear(store, noon):
    """An empty board reassures rather than inventing work."""
    text = render_panic(build_panic(store, now=noon))
    assert "Nothing is actually on fire" in text


def test_render_no_hard_clock(store, noon):
    """Only piling-up items → a calmer 'nothing has a hard clock' grounding."""
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form",
        needs_action=True, urgency="high",
    )
    text = render_panic(build_panic(store, now=noon))
    assert "nothing has a hard clock" in text.lower()
    assert "Piling up" in text


def test_render_full_triage_has_sections_and_start_here(store, noon):
    """A busy board renders the grounding line, the first step, and the buckets."""
    store.upsert_commitment(
        title="Dentist", start_at=_at(noon + timedelta(minutes=20)),
        lead_minutes=60, external_id="personal:1", hardness="hard",
    )
    store.add_todo("Submit report", deadline=noon.strftime("%Y-%m-%d"))
    text = render_panic(build_panic(store, now=noon))
    assert "# Panic mode" in text
    assert "Start here" in text
    assert "Already behind" in text
    assert "Bearing down soon" in text
    assert "actually need you right now" in text


class _FakeClient:
    def __init__(self, reply="", error=False, model="fake"):
        self.reply, self.error, self.model = reply, error, model

    def generate(self, prompt, *, system=None):
        if self.error:
            raise OllamaError("down")
        return self.reply


def test_summarize_panic_llm_and_fallback(store):
    """LLM prose when available; the deterministic triage on failure."""
    ok = summarize_panic(store, client=_FakeClient(reply="You're okay. Do this first."))
    assert ok.source == "llm" and "okay" in ok.text

    fb = summarize_panic(store, client=_FakeClient(error=True))
    assert fb.source == "heuristic" and "Panic mode" in fb.text


def test_panic_endpoint(store, noon):
    """GET /panic returns structured buckets + rendered text, token-guarded."""
    store.upsert_commitment(
        title="Dentist", start_at=_at(utcnow() + timedelta(minutes=5)),
        lead_minutes=60, external_id="personal:1", hardness="hard",
    )
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        assert c.get("/panic").status_code == 401
        body = c.get("/panic", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["late"][0]["title"] == "Leave for Dentist"
    assert body["first_step"]
    assert "Panic mode" in body["text"]
