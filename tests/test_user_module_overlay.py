"""Per-user module on/off overlay (the Settings "Features" toggles).

A user can disable a deployment-enabled module for themselves via a
``module_enabled:<key>`` coaching-state value of ``"off"``, without touching the
deployment's ``PREFRONTAL_MODULES``. The registry resolver, the coaching tick,
and the ``/settings/features`` endpoints all honor it; unset means the
deployment default (the same overlay shape as the usage-loop mute).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.registry import (
    enabled_modules,
    user_disabled_module_keys,
    user_enabled_modules,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "feat-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, modules=()):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET, modules=modules))
    return TestClient(app)


def _auth():
    return {"X-Prefrontal-Token": SECRET}


# -- the resolver ------------------------------------------------------------


def test_resolver_defaults_to_deployment(store):
    """Unset overrides => the deployment-enabled set, nothing disabled."""
    assert user_disabled_module_keys(store) == set()
    base = {m.key for m in enabled_modules(Settings(modules=()))}
    effective = {m.key for m in user_enabled_modules(store, Settings(modules=()))}
    assert effective == base


def test_off_override_drops_just_that_module(store):
    """An "off" value removes only that module from the effective set."""
    store.set_state("module_enabled:hyperfocus", "off", source="explicit")
    assert "hyperfocus" in user_disabled_module_keys(store)
    effective = {m.key for m in user_enabled_modules(store, Settings(modules=()))}
    assert "hyperfocus" not in effective
    assert "time_blindness" in effective  # others untouched


# -- the /settings/features endpoints ----------------------------------------


def test_features_get_lists_enabled_modules_all_on_by_default(store):
    with _client(store) as c:
        got = c.get("/settings/features", headers=_auth())
        assert got.status_code == 200
        mods = got.json()["modules"]
        assert mods
        assert all(m["enabled"] for m in mods)
        assert {"key", "title", "challenge", "enabled"} <= set(mods[0])
        assert "hyperfocus" in {m["key"] for m in mods}


def test_features_post_toggles_off_then_on(store):
    with _client(store) as c:
        off = c.post(
            "/settings/features", json={"modules": {"hyperfocus": False}}, headers=_auth()
        )
        assert off.status_code == 200
        assert {m["key"]: m["enabled"] for m in off.json()["modules"]}["hyperfocus"] is False
        assert store.get_state("module_enabled:hyperfocus") == "off"

        # Back on clears the override (returns to the deployment default).
        on = c.post(
            "/settings/features", json={"modules": {"hyperfocus": True}}, headers=_auth()
        )
        assert on.status_code == 200
        assert {m["key"]: m["enabled"] for m in on.json()["modules"]}["hyperfocus"] is True
        assert store.get_state("module_enabled:hyperfocus") is None


def test_features_post_ignores_unknown_keys(store):
    with _client(store) as c:
        r = c.post(
            "/settings/features", json={"modules": {"no_such_module": False}}, headers=_auth()
        )
        assert r.status_code == 200
        assert store.get_state("module_enabled:no_such_module") is None
