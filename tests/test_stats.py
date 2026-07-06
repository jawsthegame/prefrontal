"""Tests for the behavioral Insights aggregation and the /stats endpoints.

Mirrors the shape of the briefing/household tests: exercise the pure
``build_stats`` roll-up over seeded episodes (its three signature views and its
safety on an empty history), then the two HTTP surfaces — the self-contained
page shell and the auth-scoped JSON.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.modules.self_care import (
    DEFAULT_MEAL_REASK_MINUTES,
    DEFAULT_WATER_INTERVAL_MINUTES,
)
from prefrontal.stats import (
    FOLLOW_SERIES_LEN,
    MAX_ESTIMATE_POINTS,
    build_stats,
)
from prefrontal.webhooks.app import create_app


@pytest.fixture()
def store():
    """In-memory store with one provisioned user (``tester``)."""
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "tester", token="tok", is_operator=True)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def scoped(store):
    return store.scoped(store.get_user("tester")["id"])


# --- pure build_stats: empty history ----------------------------------------


def test_empty_history_is_safe_and_zeroed(scoped):
    """No episodes ⇒ counts 0, ratios/rates None, empty context/channel lists."""
    data = build_stats(scoped)
    assert data["counts"]["episodes"] == 0
    te = data["time_estimation"]
    assert te["n"] == 0 and te["ratio"] is None and te["direction"] is None
    assert te["contexts"] == [] and te["points"] == []
    ft = data["follow_through"]
    assert ft["n"] == 0 and ft["rate"] is None and ft["streak"] == 0
    assert ft["counts"] == {"success": 0, "partial": 0, "miss": 0}
    assert ft["series"] == []
    assert data["channels"] == []


# --- time estimation ---------------------------------------------------------


def test_time_estimation_bias_overall_and_per_context(scoped):
    """Median actual/predicted ratio, its direction, and per-context breakdown."""
    # Two "task" episodes running 2× over, one "departure" running on target.
    scoped.log_episode("task", predicted_value=10, actual_value=20)
    scoped.log_episode("task", predicted_value=30, actual_value=60)
    scoped.log_episode("departure", predicted_value=15, actual_value=15)
    te = build_stats(scoped)["time_estimation"]
    assert te["n"] == 3
    assert te["ratio"] == 2.0  # median of [2.0, 2.0, 1.0]
    assert te["direction"] == "over"
    by_ctx = {c["context"]: c for c in te["contexts"]}
    assert by_ctx["task"]["ratio"] == 2.0 and by_ctx["task"]["direction"] == "over"
    assert by_ctx["task"]["n"] == 2
    assert by_ctx["departure"]["ratio"] == 1.0
    assert by_ctx["departure"]["direction"] == "on"
    # contexts sorted most-used first
    assert te["contexts"][0]["context"] == "task"


def test_time_estimation_under_direction(scoped):
    """A finish-early history reads as 'under'."""
    scoped.log_episode("task", predicted_value=20, actual_value=10)
    te = build_stats(scoped)["time_estimation"]
    assert te["ratio"] == 0.5 and te["direction"] == "under"


def test_time_estimation_skips_incomplete_and_bad_pairs(scoped):
    """Episodes missing a value, or with a non-positive prediction, are ignored."""
    scoped.log_episode("task", predicted_value=10, actual_value=None)
    scoped.log_episode("task", predicted_value=None, actual_value=10)
    scoped.log_episode("task", predicted_value=0, actual_value=10)
    scoped.log_episode("task", predicted_value=10, actual_value=20)  # only valid pair
    te = build_stats(scoped)["time_estimation"]
    assert te["n"] == 1 and te["ratio"] == 2.0


def test_time_estimation_skips_switch_episodes(scoped):
    """`switch` episodes carry impulse counts (not minutes), so they're excluded
    from time-estimation accuracy and never form a bogus `switch` context."""
    scoped.log_episode("task", predicted_value=10, actual_value=20)  # the only real pair
    scoped.log_episode("switch", predicted_value=5, actual_value=1, context="focus")
    te = build_stats(scoped)["time_estimation"]
    assert te["n"] == 1 and te["ratio"] == 2.0
    assert all(c["context"] != "switch" for c in te["contexts"])


def test_time_estimation_points_are_capped(scoped):
    """The plotted points keep only the most-recent MAX_ESTIMATE_POINTS."""
    for _ in range(MAX_ESTIMATE_POINTS + 15):
        scoped.log_episode("task", predicted_value=10, actual_value=10)
    te = build_stats(scoped)["time_estimation"]
    assert len(te["points"]) == MAX_ESTIMATE_POINTS
    assert te["n"] == MAX_ESTIMATE_POINTS + 15  # summary still counts everything


# --- follow-through ----------------------------------------------------------


def test_follow_through_counts_rate_and_streak(scoped):
    """Outcome split, success rate, and the trailing-success streak."""
    for outcome in ["success", "miss", "success", "partial", "success", "success"]:
        scoped.log_episode("task", outcome=outcome)
    ft = build_stats(scoped)["follow_through"]
    assert ft["n"] == 6
    assert ft["counts"] == {"success": 4, "partial": 1, "miss": 1}
    assert ft["rate"] == round(4 / 6, 2)
    assert ft["streak"] == 2  # the two trailing successes
    assert ft["series"] == ["success", "miss", "success", "partial", "success", "success"]


def test_follow_through_streak_breaks_on_non_success(scoped):
    """A trailing miss ⇒ streak 0 even with earlier successes."""
    for outcome in ["success", "success", "miss"]:
        scoped.log_episode("task", outcome=outcome)
    ft = build_stats(scoped)["follow_through"]
    assert ft["streak"] == 0


def test_follow_through_series_is_capped(scoped):
    """The sparkline series keeps only the most-recent FOLLOW_SERIES_LEN outcomes."""
    for _ in range(FOLLOW_SERIES_LEN + 10):
        scoped.log_episode("task", outcome="success")
    ft = build_stats(scoped)["follow_through"]
    assert len(ft["series"]) == FOLLOW_SERIES_LEN
    assert ft["n"] == FOLLOW_SERIES_LEN + 10


# --- channels ----------------------------------------------------------------


def test_channel_ack_rate_per_channel_sorted_by_volume(scoped):
    """Acknowledgement rate per channel, most-used channel first."""
    scoped.log_episode("reminder", channel="notification", acknowledged=True)
    scoped.log_episode("reminder", channel="notification", acknowledged=False)
    scoped.log_episode("reminder", channel="notification", acknowledged=True)
    scoped.log_episode("reminder", channel="sms", acknowledged=True)
    rows = build_stats(scoped)["channels"]
    assert [r["channel"] for r in rows] == ["notification", "sms"]
    notif = rows[0]
    assert notif["n"] == 3 and notif["acked"] == 2
    assert notif["rate"] == round(2 / 3, 2)
    assert rows[1]["rate"] == 1.0


def test_channels_skip_episodes_without_channel_or_ack(scoped):
    """Episodes missing a channel or an acknowledged flag don't count."""
    scoped.log_episode("task", channel=None, acknowledged=True)
    scoped.log_episode("reminder", channel="sms", acknowledged=None)
    scoped.log_episode("reminder", channel="sms", acknowledged=True)
    rows = build_stats(scoped)["channels"]
    assert len(rows) == 1 and rows[0]["channel"] == "sms" and rows[0]["n"] == 1


# --- self-care ---------------------------------------------------------------


def _log_self_care(scoped, key, outcome, notes):
    """Log one self-care response the way the module does (context = '<key>: <outcome>')."""
    scoped.log_episode(
        "self_care",
        acknowledged=outcome != "ignored",
        context=f"{key}: {outcome}",
        outcome=outcome,
        notes=notes,
    )


def test_self_care_empty_history_is_zeroed(scoped):
    """No self-care episodes ⇒ one zeroed row per check, in order, at default cadence."""
    rows = build_stats(scoped)["self_care"]
    assert [r["key"] for r in rows] == ["meal", "water", "meds"]
    for r in rows:
        assert r["n"] == 0
        assert r["confirmed"] == 0 and r["snoozed"] == 0 and r["ignored"] == 0
        assert r["response_rate"] is None
        assert r["avg_latency_seconds"] is None
        # interval falls back to the check's default when unset
        assert r["interval_minutes"] == r["default_interval_minutes"]
    meal = rows[0]
    assert meal["default_interval_minutes"] == DEFAULT_MEAL_REASK_MINUTES


def test_self_care_counts_rate_and_latency(scoped):
    """A confirmed/snoozed/ignored mix yields the right split, rate, and avg latency."""
    _log_self_care(scoped, "meal", "confirmed", "1/1 latency=10s")
    _log_self_care(scoped, "meal", "confirmed", "1/1 latency=20s")
    _log_self_care(scoped, "meal", "snoozed", "30m latency=5s")
    _log_self_care(scoped, "meal", "ignored", "latency=?")
    # A response for another check must not bleed into meal's row.
    _log_self_care(scoped, "water", "confirmed", "1/6 latency=99s")
    rows = {r["key"]: r for r in build_stats(scoped)["self_care"]}
    meal = rows["meal"]
    assert meal["n"] == 4
    assert meal["confirmed"] == 2 and meal["snoozed"] == 1 and meal["ignored"] == 1
    assert meal["response_rate"] == 0.5  # 2 confirmed / 4
    # avg latency over the two confirmed taps that carry a numeric latency only
    assert meal["avg_latency_seconds"] == 15.0
    # water saw exactly its one confirm; meds saw nothing
    assert rows["water"]["confirmed"] == 1 and rows["water"]["n"] == 1
    assert rows["meds"]["n"] == 0


def test_self_care_avg_latency_none_without_timed_confirms(scoped):
    """Confirms with no numeric latency (latency=?) leave avg_latency None."""
    _log_self_care(scoped, "meds", "confirmed", "1/1 latency=?")
    _log_self_care(scoped, "meds", "snoozed", "30m latency=40s")  # snooze latency ignored
    meds = {r["key"]: r for r in build_stats(scoped)["self_care"]}["meds"]
    assert meds["confirmed"] == 1 and meds["snoozed"] == 1
    assert meds["response_rate"] == 0.5
    assert meds["avg_latency_seconds"] is None


def test_self_care_interval_reflects_coaching_state_override(scoped):
    """A learned/explicit interval override is surfaced against the default."""
    scoped.set_state("water_interval_minutes", "120", source="explicit")
    water = {r["key"]: r for r in build_stats(scoped)["self_care"]}["water"]
    assert water["interval_minutes"] == 120
    assert water["default_interval_minutes"] == DEFAULT_WATER_INTERVAL_MINUTES


# --- endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        yield c


def test_stats_page_served_without_auth(client):
    """The shell is a self-contained HTML page carrying no data."""
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Insights" in body
    assert "/stats/data" in body  # it fetches the JSON
    assert "X-Prefrontal-Token" in body  # and signs in
    # reachable from the shared nav on every page
    for page in ("/family", "/kids", "/dashboard"):
        assert '<a href="/stats">Insights</a>' in client.get(page).text


def test_stats_data_requires_auth(client):
    """The JSON is per-user and refuses an unauthenticated request."""
    assert client.get("/stats/data").status_code == 401


def test_stats_data_returns_scoped_aggregates(client, scoped):
    """/stats/data rolls up the signed-in user's own episodes."""
    scoped.log_episode("task", predicted_value=10, actual_value=20, outcome="success")
    scoped.log_episode("reminder", channel="sms", acknowledged=True, outcome="miss")
    resp = client.get("/stats/data", headers={"X-Prefrontal-Token": "tok"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["episodes"] == 2
    assert data["time_estimation"]["ratio"] == 2.0
    assert data["follow_through"]["counts"]["success"] == 1
    assert data["channels"][0]["channel"] == "sms"


def test_stats_data_is_isolated_between_users(client, store):
    """One user's episodes never show up in another user's insights."""
    provision_user(store, "other", token="other-tok")
    other = store.scoped(store.get_user("other")["id"])
    other.log_episode("task", predicted_value=10, actual_value=99, outcome="success")
    resp = client.get("/stats/data", headers={"X-Prefrontal-Token": "tok"})
    assert resp.json()["counts"]["episodes"] == 0
