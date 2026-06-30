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
