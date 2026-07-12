"""Tests for the Phase-2 feature-usage loop: mute + the weekly usage nudge.

Covers the mute storage, the coaching-tick mute filter, the weekly nudge
selection + ISO-week dedup, the one-tap mute/keep handler, and the
``POST /webhooks/usage/check`` endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import fmt_ts, utcnow
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.usage import (
    NUDGE_PENDING_KEY,
    NUDGE_WEEK_KEY,
    apply_usage_decision,
    build_usage_nudge,
    run_usage_check,
)
from prefrontal.webhooks.app import create_app

SECRET = "tok"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "tom", token=SECRET, is_operator=True)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def scoped(store):
    return store.scoped(store.get_user("tom")["id"])


def _ignored(scoped, feature, n=6):
    """Make `feature` look firing-but-ignored: n offered, 0 engaged."""
    for i in range(n):
        scoped.record_feature_event(feature, "offered", ref=str(i))


# --- mute storage -----------------------------------------------------------


def test_mute_storage_roundtrip(scoped):
    assert scoped.muted_features() == set()
    assert scoped.set_feature_muted("hyperfocus", True) is True
    assert scoped.muted_features() == {"hyperfocus"}
    scoped.set_feature_muted("hyperfocus", False)
    assert scoped.muted_features() == set()


def test_muted_module_dropped_from_coaching_tick(scoped, monkeypatch):
    import prefrontal.coaching as coaching

    seen: dict[str, list[str]] = {}
    original = coaching.collect_cues

    def spy(store, modules, ctx):
        seen["keys"] = [m.key for m in modules]
        return original(store, modules, ctx)

    monkeypatch.setattr(coaching, "collect_cues", spy)
    scoped.set_feature_muted("hyperfocus", True)
    settings = Settings(timezone="UTC")
    ollama = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
    coaching.run_coaching_tick(scoped, settings=settings, ollama=ollama)
    assert "hyperfocus" not in seen["keys"]
    assert "task_paralysis" in seen["keys"]  # an unmuted module still fans in


# --- nudge selection --------------------------------------------------------


def test_no_nudge_when_nothing_ignored(scoped):
    assert build_usage_nudge(scoped, Settings()) is None


def test_nudge_picks_worst_ignored_module(scoped):
    _ignored(scoped, "location_anchor", n=6)  # 0% acted on
    # impulsivity: 5 offered, 1 engaged = 20% → above the ignored threshold, so
    # not a candidate; location_anchor (0%) is the pick.
    for i in range(5):
        scoped.record_feature_event("impulsivity", "offered", ref=str(i))
    scoped.record_feature_event("impulsivity", "engaged")
    nudge = build_usage_nudge(scoped, Settings())
    assert nudge is not None and nudge["feature"] == "location_anchor"
    assert "Location" in nudge["title"]


def test_nudge_skips_muted_and_pull_surfaces(scoped):
    _ignored(scoped, "location_anchor")
    # A pull surface with lots of "invoked" is never a mute candidate.
    for _ in range(9):
        scoped.record_feature_event("panic", "invoked")
    scoped.set_feature_muted("location_anchor", True)
    assert build_usage_nudge(scoped, Settings()) is None


def test_nudge_skips_recently_kept(scoped):
    _ignored(scoped, "location_anchor")
    scoped.set_state("usage_kept:location_anchor", fmt_ts(utcnow()), source="inferred")
    assert build_usage_nudge(scoped, Settings()) is None


# --- weekly sweep -----------------------------------------------------------


def test_run_check_stashes_pending_and_dedups_by_week(scoped):
    _ignored(scoped, "location_anchor")
    # deliver=True; with no ntfy configured the transport is a no-op, but the
    # pending stash + week stamp (the loop's state) still happen.
    first = run_usage_check(scoped, settings=Settings(), handle="tom", deliver=True)
    assert first["feature"] == "location_anchor"
    assert scoped.get_state(NUDGE_PENDING_KEY) == "location_anchor"
    assert scoped.get_state(NUDGE_WEEK_KEY)  # stamped
    # Same week → no second nudge.
    second = run_usage_check(scoped, settings=Settings(), handle="tom", deliver=True)
    assert second["delivered"] is False and "week" in second["reason"]


def test_dry_run_check_is_side_effect_free(scoped):
    _ignored(scoped, "location_anchor")
    report = run_usage_check(scoped, settings=Settings(), handle="tom", deliver=False)
    assert report["feature"] == "location_anchor" and report["delivered"] is False
    # A preview must not stash a pending target or burn the weekly slot.
    assert not scoped.get_state(NUDGE_PENDING_KEY)
    assert not scoped.get_state(NUDGE_WEEK_KEY)


# --- one-tap mute / keep ----------------------------------------------------


def test_one_tap_mute_mutes_and_clears_pending(scoped):
    scoped.set_state(NUDGE_PENDING_KEY, "location_anchor", source="inferred")
    line = apply_usage_decision(scoped, "usage_mute")
    assert "Location" in line  # confirms with the module's human title
    assert "location_anchor" in scoped.muted_features()
    assert not scoped.get_state(NUDGE_PENDING_KEY)
    # A second tap has nothing pending → friendly no-op, no error.
    again = apply_usage_decision(scoped, "usage_mute")
    assert "already" in again.lower()


def test_one_tap_keep_snoozes_without_muting(scoped):
    scoped.set_state(NUDGE_PENDING_KEY, "location_anchor", source="inferred")
    apply_usage_decision(scoped, "usage_keep")
    assert "location_anchor" not in scoped.muted_features()
    assert scoped.get_state("usage_kept:location_anchor")  # snooze stamped
    assert not scoped.get_state(NUDGE_PENDING_KEY)


def test_usage_actions_are_signable():
    # The one-tap links only verify if the actions are in the allowlist.
    from prefrontal.webhooks.oauth import sign_action, verify_action

    for action in ("usage_mute", "usage_keep"):
        token = sign_action("tom", action, 0, SECRET)
        assert verify_action(token, SECRET) == ("tom", action, 0)


# --- endpoint ---------------------------------------------------------------


def test_usage_check_endpoint(store):
    scoped = store.scoped(store.get_user("tom")["id"])
    _ignored(scoped, "location_anchor")
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        r = c.post("/webhooks/usage/check", headers={"X-Prefrontal-Token": SECRET})
    assert r.status_code == 200
    body = r.json()
    assert body["feature"] == "location_anchor"
    assert scoped.get_state(NUDGE_PENDING_KEY) == "location_anchor"
