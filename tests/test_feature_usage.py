"""Tests for the feature-usage feedback loop.

Covers the storage spine (``feature_events`` + ``feature_usage_rollup``), the
``_feature_usage`` roll-up's bucketing (using / ignored / dormant), and the four
capture seams end-to-end through their real entry points: ``offered`` via
``record_fired``, ``engaged`` via ``apply_nudge_action`` and the Shortcut webhook,
and ``invoked`` via the HTTP middleware.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import utcnow
from prefrontal.coaching import Cue, Decision, record_fired
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.nudges import apply_nudge_action
from prefrontal.stats import (
    USAGE_IGNORED_MIN_OFFERED,
    _feature_usage,
    build_stats,
)
from prefrontal.webhooks.app import create_app

SECRET = "tok"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "tester", token=SECRET, is_operator=True)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def scoped(store):
    return store.scoped(store.get_user("tester")["id"])


# --- storage spine ----------------------------------------------------------


def test_record_rejects_unknown_event(scoped):
    with pytest.raises(ValueError):
        scoped.record_feature_event("panic", "looked-at")
    assert scoped.feature_usage_rollup(30) == []


def test_record_and_rollup_counts_by_event(scoped):
    scoped.record_feature_event("panic", "invoked", source="cli")
    scoped.record_feature_event("panic", "invoked", source="http")
    scoped.record_feature_event("self_care", "offered", intervention="meal")
    scoped.record_feature_event("self_care", "engaged", intervention="meal_ate")
    roll = {r["feature"]: r for r in scoped.feature_usage_rollup(30)}
    assert roll["panic"]["invoked"] == 2
    assert roll["self_care"]["offered"] == 1 and roll["self_care"]["engaged"] == 1


# --- bucketing --------------------------------------------------------------


def test_empty_history_is_all_dormant_and_safe(scoped):
    fu = _feature_usage(scoped)
    assert fu["summary"]["using"] == 0 and fu["summary"]["ignored"] == 0
    assert fu["summary"]["dormant"] > 0  # every enabled module + pull surface
    assert all(f["bucket"] == "dormant" for f in fu["features"])
    # And it's wired into the top-level payload.
    assert "feature_usage" in build_stats(scoped)


def test_push_feature_that_fires_but_is_ignored_is_flagged(scoped):
    for i in range(USAGE_IGNORED_MIN_OFFERED + 1):
        scoped.record_feature_event("location_anchor", "offered", ref=str(i))
    fu = {f["feature"]: f for f in _feature_usage(scoped)["features"]}
    assert fu["location_anchor"]["bucket"] == "ignored"
    assert fu["location_anchor"]["engagement_rate"] == 0.0


def test_engaged_and_invoked_features_read_as_using(scoped):
    scoped.record_feature_event("self_care", "offered", intervention="meal")
    scoped.record_feature_event("self_care", "engaged", intervention="meal_ate")
    scoped.record_feature_event("briefing", "invoked", source="cli")
    fu = {f["feature"]: f for f in _feature_usage(scoped)["features"]}
    assert fu["self_care"]["bucket"] == "using"
    assert fu["briefing"]["bucket"] == "using"


def test_low_offer_count_is_not_yet_ignored(scoped):
    # Below the min-offered floor there isn't enough signal to call it noise.
    scoped.record_feature_event("impulsivity", "offered")
    fu = {f["feature"]: f for f in _feature_usage(scoped)["features"]}
    assert fu["impulsivity"]["bucket"] == "using"


# --- capture seams (real entry points) --------------------------------------


def test_record_fired_stamps_offered_with_module_and_intervention(scoped):
    cue = Cue(
        module="hyperfocus", intervention="protect_focus", urgency="nudge",
        text="on track?", context_key="focus", dedup_key="hf:1",
    )
    record_fired(scoped, [Decision(cue=cue, channel="push", text="on track?")], utcnow())
    roll = {r["feature"]: r for r in scoped.feature_usage_rollup(30)}
    assert roll["hyperfocus"]["offered"] == 1


def test_one_tap_action_stamps_engaged(scoped):
    user = scoped.get_user("tester")
    apply_nudge_action(scoped, "meal_ate", 0, user=user, settings=Settings())
    roll = {r["feature"]: r for r in scoped.feature_usage_rollup(30)}
    assert roll["self_care"]["engaged"] == 1


def test_unknown_one_tap_action_is_not_recorded(scoped):
    # An action the dispatcher doesn't recognize no-ops; it must not pollute stats.
    user = scoped.get_user("tester")
    apply_nudge_action(scoped, "not_a_real_action", 0, user=user, settings=Settings())
    assert scoped.feature_usage_rollup(30) == []


def test_http_middleware_stamps_invoked_for_pull_surface(store):
    app = create_app(store=store, settings=Settings())
    scoped = store.scoped(store.get_user("tester")["id"])
    with TestClient(app) as c:
        assert c.get("/stats/data", headers={"X-Prefrontal-Token": SECRET}).status_code == 200
    roll = {r["feature"]: r for r in scoped.feature_usage_rollup(30)}
    assert roll["stats"]["invoked"] == 1


def test_shortcut_webhook_stamps_engaged(store):
    app = create_app(store=store, settings=Settings())
    scoped = store.scoped(store.get_user("tester")["id"])
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/shortcut",
            headers={"X-Prefrontal-Token": SECRET},
            json={"action": "made_it", "episode_type": "departure", "channel": "notification"},
        )
        assert r.status_code == 201
    roll = {r["feature"]: r for r in scoped.feature_usage_rollup(30)}
    assert roll["departure"]["engaged"] == 1


def test_non_pull_get_is_not_recorded(store):
    app = create_app(store=store, settings=Settings())
    scoped = store.scoped(store.get_user("tester")["id"])
    with TestClient(app) as c:
        c.get("/health")  # not a curated pull surface
    assert scoped.feature_usage_rollup(30) == []
