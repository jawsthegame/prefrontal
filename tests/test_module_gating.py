"""Module enablement gates proactive interventions at runtime.

Regression for the abstraction gap where `PREFRONTAL_MODULES` only affected the
profile document and seeded defaults — a disabled module's webhook nudges still
fired. The proactive "check" endpoints now consult `registry.is_enabled`, so a
disabled module's interventions are actually suppressed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.registry import is_enabled
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "gate-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, modules):
    app = create_app(
        store=store, settings=Settings(webhook_secret=SECRET, modules=modules)
    )
    return TestClient(app)


def _auth():
    return {"X-Prefrontal-Token": SECRET}


# -- the registry helper -----------------------------------------------------


def test_is_enabled_semantics():
    """Empty config enables all; an explicit list gates; unknown keys are off."""
    assert is_enabled("hyperfocus", Settings(modules=()))  # empty => all on
    assert is_enabled("hyperfocus", Settings(modules=("hyperfocus",)))
    assert not is_enabled("hyperfocus", Settings(modules=("time_blindness",)))
    assert not is_enabled("no_such_module", Settings(modules=()))


# -- runtime gating of proactive interventions -------------------------------


def test_focus_check_skipped_when_hyperfocus_disabled(store):
    """/webhooks/focus/check no-ops when hyperfocus is not enabled."""
    with _client(store, modules=("time_blindness",)) as c:
        resp = c.post("/webhooks/focus/check", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] == "module_disabled"
    assert body["active"] == []


def test_outing_check_skipped_when_location_anchor_disabled(store):
    """/webhooks/outing/check no-ops when location_anchor is not enabled."""
    with _client(store, modules=("time_blindness",)) as c:
        resp = c.post("/webhooks/outing/check", json={}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "module_disabled"


def test_departure_check_skipped_when_time_blindness_disabled(store):
    """/webhooks/departure/check no-ops (never fires) when time_blindness is off."""
    with _client(store, modules=("hyperfocus",)) as c:
        resp = c.post("/webhooks/departure/check", json={}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] == "module_disabled"
    assert body["fire"] is False


def test_enabled_module_is_not_skipped(store):
    """With the owning module enabled, the endpoint runs (no skip marker)."""
    with _client(store, modules=("hyperfocus",)) as c:
        resp = c.post("/webhooks/focus/check", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert "skipped" not in body
    assert body["active"] == []  # no active sessions, but the check ran


def test_empty_config_enables_everything(store):
    """The fresh-install default (empty modules) leaves all interventions live."""
    with _client(store, modules=()) as c:
        assert "skipped" not in c.post("/webhooks/focus/check", headers=_auth()).json()
        assert (
            "skipped"
            not in c.post("/webhooks/outing/check", json={}, headers=_auth()).json()
        )
