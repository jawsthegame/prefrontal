"""Tests for the SQLite behavioral memory layer.

Covers schema initialization and seeding, episode logging/querying, pattern
upsert semantics, coaching-state round-trips, and the heuristic summarizer.
All tests run against an in-memory database so they leave no files behind.
"""

from __future__ import annotations

import pytest

from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile

# The seven seed rows defined in schema.sql / docs/schema.md.
SEED_KEYS = {
    "preferred_briefing_format",
    "escalation_delay_minutes",
    "responsive_hours_start",
    "responsive_hours_end",
    "preferred_reminder_channel",
    "time_estimation_bias",
    "active_escalation_path",
}


@pytest.fixture()
def store():
    """Yield a MemoryStore backed by a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield s


def test_schema_creates_three_tables(store):
    """init_db should create exactly the episodes, patterns, coaching_state tables."""
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"episodes", "patterns", "coaching_state"} <= names


def test_seed_rows_present(store):
    """All seven coaching_state seed rows should exist with known values."""
    state = store.all_state()
    assert SEED_KEYS <= set(state)
    assert state["time_estimation_bias"]["value"] == "1.4"
    assert state["preferred_briefing_format"]["source"] == "explicit"


def test_seed_is_idempotent_and_non_clobbering():
    """Re-initializing must not duplicate rows or overwrite user-changed values."""
    with MemoryStore.open(":memory:") as store:
        store.set_state("time_estimation_bias", "1.6", source="explicit")
        # Re-applying the schema (init_db) again on the same connection.
        from prefrontal.memory.db import SCHEMA_PATH

        store.conn.executescript(SCHEMA_PATH.read_text())
        store.conn.commit()
        # Value the user changed is preserved; no duplicate keys.
        assert store.get_state("time_estimation_bias") == "1.6"
        count = store.conn.execute(
            "SELECT COUNT(*) AS c FROM coaching_state WHERE key='time_estimation_bias'"
        ).fetchone()["c"]
        assert count == 1


def test_log_and_get_episode(store):
    """An inserted episode round-trips with all fields intact."""
    eid = store.log_episode(
        "departure",
        predicted_value=15.0,
        actual_value=21.0,
        acknowledged=True,
        channel="notification",
        context="leaving home, morning",
        outcome="miss",
        notes="traffic on the bridge",
    )
    assert eid > 0
    ep = store.get_episode(eid)
    assert ep["episode_type"] == "departure"
    assert ep["actual_value"] == 21.0
    assert ep["outcome"] == "miss"
    assert ep["timestamp"] is not None  # DB default applied


def test_episodes_by_type_filters_and_orders(store):
    """episodes_by_type returns only the requested type, newest first."""
    store.log_episode("departure", timestamp="2026-01-01T08:00:00")
    store.log_episode("task", timestamp="2026-01-01T09:00:00")
    store.log_episode("departure", timestamp="2026-01-02T08:00:00")

    departures = store.episodes_by_type("departure")
    assert len(departures) == 2
    assert all(e["episode_type"] == "departure" for e in departures)
    assert departures[0]["timestamp"] > departures[1]["timestamp"]


def test_recent_episodes_limit(store):
    """recent_episodes honors its limit and orders newest first."""
    for i in range(5):
        store.log_episode("checkin", timestamp=f"2026-01-0{i + 1}T08:00:00")
    recent = store.recent_episodes(limit=3)
    assert len(recent) == 3
    assert recent[0]["timestamp"] == "2026-01-05T08:00:00"


def test_upsert_pattern_inserts_then_updates(store):
    """upsert_pattern creates one row per (type, key) and updates it in place."""
    pid1 = store.upsert_pattern(
        "time_estimation", "departure", observed_value=21.0, predicted_value=15.0,
        variance=6.0, sample_size=3, confidence=0.4,
    )
    pid2 = store.upsert_pattern(
        "time_estimation", "departure", observed_value=20.0, predicted_value=15.0,
        variance=5.0, sample_size=8, confidence=0.7,
    )
    assert pid1 == pid2  # same row, updated in place
    patterns = store.get_patterns("time_estimation")
    assert len(patterns) == 1
    assert patterns[0]["sample_size"] == 8
    assert patterns[0]["confidence"] == 0.7


def test_coaching_state_round_trip(store):
    """set_state inserts and updates; get_state honors defaults for missing keys."""
    assert store.get_state("nope", default="fallback") == "fallback"
    store.set_state("preferred_reminder_channel", "tts", source="explicit")
    assert store.get_state("preferred_reminder_channel") == "tts"
    assert store.all_state()["preferred_reminder_channel"]["source"] == "explicit"


def test_build_profile_includes_state_and_bias(store):
    """The profile surfaces coaching preferences and the time-estimation multiplier."""
    profile = build_profile(store)
    assert "Behavioral profile" in profile
    assert "preferred_briefing_format" in profile
    assert "1.4x multiplier" in profile  # derived from the seed bias of 1.4


def test_build_profile_lists_patterns(store):
    """Derived patterns appear in the profile once present."""
    store.upsert_pattern(
        "time_estimation", "departure", observed_value=21.0, predicted_value=15.0,
        variance=6.0, sample_size=8, confidence=0.7,
    )
    profile = build_profile(store)
    assert "time_estimation" in profile
    assert "departure" in profile
