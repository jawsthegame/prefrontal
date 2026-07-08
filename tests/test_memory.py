"""Tests for the SQLite behavioral memory layer.

Covers schema initialization and seeding, episode logging/querying, pattern
upsert semantics, coaching-state round-trips, and the heuristic summarizer.
All tests run against an in-memory database so they leave no files behind.
"""

from __future__ import annotations

import pytest

from prefrontal.memory.store import MemoryStore, seed_user_state
from prefrontal.memory.summarizer import build_profile
from tests.conftest import scoped_default

# The coaching_state seed rows defined in schema.sql / docs/schema.md. (Modules
# seed their own additional keys when enabled; these are the schema-level seeds
# that always exist after init_db.)
SEED_KEYS = {
    "preferred_briefing_format",
    "escalation_delay_minutes",
    "responsive_hours_start",
    "responsive_hours_end",
    "preferred_reminder_channel",
    "time_estimation_bias",
    "active_escalation_path",
    "travel_speed_kmh",
    "travel_road_factor",
    "departure_prep_minutes",
    "departure_heads_up_minutes",
    "departure_soon_minutes",
    "geocoding_enabled",
}


@pytest.fixture()
def store():
    """Yield a user-scoped MemoryStore on a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def test_schema_creates_core_tables(store):
    """init_db should create at least the three core tables (plus feature tables)."""
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"episodes", "patterns", "coaching_state"} <= names


def test_seed_rows_present(store):
    """Every schema-level coaching_state seed row should exist with known values."""
    state = store.all_state()
    assert SEED_KEYS <= set(state)
    assert state["time_estimation_bias"]["value"] == "1.4"
    assert state["preferred_briefing_format"]["source"] == "explicit"


def test_seed_is_idempotent_and_non_clobbering():
    """Re-seeding a user's defaults must not duplicate rows or overwrite user values."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("time_estimation_bias", "1.6", source="explicit")
        # Re-run the *per-user* seed — the real non-clobber path. (The old test
        # re-ran schema.sql, which no longer seeds coaching_state, so its
        # assertions could not fail regardless of any seeding regression.)
        seed_user_state(store)
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


def test_get_float_parses_and_falls_back(store):
    """get_float parses numeric state and falls back on missing/unparseable values."""
    assert store.get_float("missing", 1.5) == 1.5  # absent key -> default
    store.set_state("travel_speed_kmh", "42", source="explicit")
    assert store.get_float("travel_speed_kmh", 30.0) == 42.0
    store.set_state("travel_speed_kmh", "not-a-number", source="explicit")
    assert store.get_float("travel_speed_kmh", 30.0) == 30.0  # bad value -> default


def test_get_bool_recognizes_tokens_and_falls_back(store):
    """get_bool maps known truthy/falsey tokens; unknown/missing -> default."""
    assert store.get_bool("missing", True) is True  # absent key -> default
    for token in ("1", "true", "TRUE", "Yes", "on"):
        store.set_state("flag", token, source="explicit")
        assert store.get_bool("flag", False) is True
    for token in ("0", "false", "No", "off"):
        store.set_state("flag", token, source="explicit")
        assert store.get_bool("flag", True) is False
    store.set_state("flag", "maybe", source="explicit")
    assert store.get_bool("flag", True) is True  # unrecognized -> default


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


def test_profile_cache_empty_by_default(store):
    """A fresh DB has no cached narrative."""
    assert store.get_profile_cache() is None


def test_profile_cache_round_trip_and_hash(store):
    """set_profile_cache stores the row, derives the hash, and upserts in place."""
    import hashlib

    structured = "# Behavioral profile\n\nfacts"
    store.set_profile_cache(
        "coaching prose", source="llm", model="qwen2.5:14b", structured=structured
    )
    row = store.get_profile_cache()
    assert row["text"] == "coaching prose"
    assert row["source"] == "llm"
    assert row["model"] == "qwen2.5:14b"
    assert row["structured"] == structured
    assert row["structured_hash"] == hashlib.sha256(structured.encode()).hexdigest()
    assert row["generated_at"]  # populated by the DB clock

    # A second write replaces the single row rather than inserting a new one.
    store.set_profile_cache(
        "newer prose", source="heuristic", model=None, structured="# Behavioral profile\n"
    )
    assert store.get_profile_cache()["text"] == "newer prose"
    assert store.get_profile_cache()["model"] is None
    count = store.conn.execute("SELECT COUNT(*) AS n FROM profile_cache").fetchone()["n"]
    assert count == 1
