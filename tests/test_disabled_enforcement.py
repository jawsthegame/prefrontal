"""Turning a module off must disable it on *every* fire path, not just the tick.

The per-user Settings ▸ Features overlay (``module_enabled:<key>`` = ``"off"``) and
the usage-loop mute are twins: both mean "don't act for me". Mute was already
honored by the standalone intervention entry points (the webhook "check" routes);
the enable overlay was not, so a module switched off in the app kept firing from
those endpoints (the n8n poll deployment path) while going quiet in the native
coaching tick. These tests pin the contract for both switches, plus the surfaces
that are supposed to follow deployment-off (the Guide, the leave-by pull, the
weekly usage nudge, and zero-tap focus arming).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import TS_FMT
from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.hyperfocus import arm_focus_session
from prefrontal.modules.registry import (
    MODULE_ENABLED_PREFIX,
    user_module_enabled,
    user_module_off,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "disable-secret"

#: The three module-owned "check" endpoints and the module each one belongs to.
CHECK_ROUTES = (
    ("/webhooks/focus/check", "hyperfocus"),
    ("/webhooks/outing/check", "location_anchor"),
    ("/webhooks/departure/check", "time_blindness"),
)


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, *, modules=(), packs=()):
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET, modules=modules, packs=packs),
    )
    client = TestClient(app)
    client.__enter__()
    return client


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _turn_off(store, key: str) -> None:
    """Flip the per-user Settings ▸ Features switch off for ``key``."""
    store.set_state(f"{MODULE_ENABLED_PREFIX}{key}", "off", source="explicit")


# -- the single-key resolvers ------------------------------------------------


def test_user_module_off_reads_the_overlay(store):
    assert user_module_off(store, "hyperfocus") is False
    _turn_off(store, "hyperfocus")
    assert user_module_off(store, "hyperfocus") is True
    # Only that key.
    assert user_module_off(store, "time_blindness") is False


def test_user_module_off_tolerates_a_storeless_double():
    class Broken:
        def get_state(self, key):  # noqa: D102, ANN001
            raise RuntimeError("no state repo")

    # Best-effort: a failing read must read as "not off" so no nudge path breaks.
    assert user_module_off(Broken(), "hyperfocus") is False


def test_user_module_enabled_combines_both_switches(store):
    settings = Settings(modules=())
    assert user_module_enabled(store, "hyperfocus", settings) is True
    _turn_off(store, "hyperfocus")
    assert user_module_enabled(store, "hyperfocus", settings) is False
    # Deployment-off is enough on its own, overlay or not.
    assert user_module_enabled(store, "hyperfocus", Settings(modules=("projects",))) is False
    # An unknown key is disabled, never a KeyError.
    assert user_module_enabled(store, "no_such_module", settings) is False


# -- the intervention entry points (the "check" routes) ----------------------


@pytest.mark.parametrize(("route", "key"), CHECK_ROUTES)
def test_check_route_fires_when_the_module_is_on(store, route, key):
    """Control: with everything on, the route runs (no ``skipped`` marker)."""
    client = _client(store)
    body = client.post(route, headers=_auth(), json={}).json()
    assert "skipped" not in body


@pytest.mark.parametrize(("route", "key"), CHECK_ROUTES)
def test_check_route_honors_deployment_off(store, route, key):
    client = _client(store, modules=("projects",))
    body = client.post(route, headers=_auth(), json={}).json()
    assert body["skipped"] == "module_disabled"


@pytest.mark.parametrize(("route", "key"), CHECK_ROUTES)
def test_check_route_honors_the_usage_mute(store, route, key):
    store.set_state("usage_muted_features", key, source="explicit")
    client = _client(store)
    body = client.post(route, headers=_auth(), json={}).json()
    assert body["skipped"] == "module_muted"


@pytest.mark.parametrize(("route", "key"), CHECK_ROUTES)
def test_check_route_honors_the_per_user_features_switch(store, route, key):
    """The regression this suite exists for: "off" in the app silences the poll."""
    _turn_off(store, key)
    client = _client(store)
    body = client.post(route, headers=_auth(), json={}).json()
    assert body["skipped"] == "module_off"


def test_disabled_hyperfocus_check_claims_no_protection(store):
    """A module the user turned off must not shield other modules' nudges either."""
    _turn_off(store, "hyperfocus")
    client = _client(store)
    body = client.post("/webhooks/focus/check", headers=_auth(), json={}).json()
    assert body["protect"] is False
    assert body["active"] == []


# -- zero-tap focus arming ---------------------------------------------------


def _live_focus_block(store) -> None:
    now = utcnow()
    store.upsert_commitment(
        title="Focus: write the design doc",
        start_at=(now - timedelta(minutes=5)).strftime(TS_FMT),
        end_at=(now + timedelta(minutes=55)).strftime(TS_FMT),
    )


def test_focus_arm_works_when_hyperfocus_is_on(store):
    _live_focus_block(store)
    assert arm_focus_session(store, Settings(modules=()))["armed"] is True


def test_focus_arm_respects_the_per_user_switch(store):
    """`prefrontal focus arm` runs every 60s; it must not arm a disabled module."""
    _live_focus_block(store)
    _turn_off(store, "hyperfocus")
    result = arm_focus_session(store, Settings(modules=()))
    assert result["armed"] is False
    assert "off" in result["reason"]
    assert store.active_focus_sessions() == []


def test_focus_arm_respects_deployment_off(store):
    _live_focus_block(store)
    result = arm_focus_session(store, Settings(modules=("projects",)))
    assert result["armed"] is False
    assert store.active_focus_sessions() == []


def test_focus_arm_endpoint_respects_the_per_user_switch(store):
    _live_focus_block(store)
    _turn_off(store, "hyperfocus")
    client = _client(store)
    body = client.post("/webhooks/focus/arm", headers=_auth(), json={}).json()
    assert body["armed"] is False


# -- read surfaces that already follow deployment-off ------------------------


def test_guide_hides_a_module_the_user_turned_off(store):
    client = _client(store)
    on = client.get("/guide/data", headers=_auth()).json()
    assert "hyperfocus" in {m["key"] for m in on["modules"]}
    _turn_off(store, "hyperfocus")
    off = client.get("/guide/data", headers=_auth()).json()
    keys = {m["key"] for m in off["modules"]}
    assert "hyperfocus" not in keys
    assert off["total"] == on["total"] - 1


def test_departure_next_hides_the_leave_by_when_time_blindness_is_off(store):
    """The widget/Today pull mirrors the nudge: off means no leave-by."""
    now = utcnow()
    store.upsert_commitment(
        title="Dentist",
        start_at=(now + timedelta(hours=2)).strftime(TS_FMT),
        location="123 Main St",
        lead_minutes=20.0,
    )
    client = _client(store)
    assert client.get("/departure/next", headers=_auth()).json()["departure"] is not None
    _turn_off(store, "time_blindness")
    assert client.get("/departure/next", headers=_auth()).json()["departure"] is None


def test_balance_hint_reports_trip_tracking_off_for_this_user(store):
    """The empty-view explanation reflects the user's own switch, not just config."""
    # The hint only speaks up for a user who expects the guardrail (a weekly aim or
    # the nudge flag), so arm it the way the Parent pack does.
    store.set_state("focus_balance_nudge", "1", source="explicit")
    _turn_off(store, "trip_tracking")
    client = _client(store)
    hint = client.get("/balance", headers=_auth()).json()["hint"] or ""
    assert "trip" in hint.lower()


# -- the weekly usage nudge --------------------------------------------------


def test_usage_nudge_skips_a_module_the_user_already_turned_off(store):
    """Don't offer to mute what's already off — the history outlives the switch."""
    from prefrontal.stats import USAGE_IGNORED_MIN_OFFERED
    from prefrontal.usage import build_usage_nudge

    for _ in range(USAGE_IGNORED_MIN_OFFERED + 2):
        store.record_feature_event("hyperfocus", "offered")
    settings = Settings(modules=())
    assert build_usage_nudge(store, settings)["feature"] == "hyperfocus"
    _turn_off(store, "hyperfocus")
    assert build_usage_nudge(store, settings) is None


# -- the tick and its dry-run preview agree ---------------------------------


def test_effective_modules_drops_muted_and_user_off(store):
    from prefrontal.coaching import effective_modules

    settings = Settings(modules=())
    base = {m.key for m in effective_modules(store, settings)}
    assert {"hyperfocus", "time_blindness"} <= base
    store.set_state("usage_muted_features", "time_blindness", source="explicit")
    _turn_off(store, "hyperfocus")
    left = {m.key for m in effective_modules(store, settings)}
    assert "hyperfocus" not in left
    assert "time_blindness" not in left
    assert len(left) == len(base) - 2


def test_effective_modules_survives_a_store_without_the_repos():
    """Best-effort: a bad store yields the deployment set, never an exception."""
    from prefrontal.coaching import effective_modules

    class Broken:
        def get_state(self, key):  # noqa: ANN001, D102
            raise RuntimeError("nope")

        def muted_features(self):  # noqa: D102
            raise RuntimeError("nope")

    keys = {m.key for m in effective_modules(Broken(), Settings(modules=()))}
    assert "hyperfocus" in keys
