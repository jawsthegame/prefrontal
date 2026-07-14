"""Per-user pack on/off overlay (the Settings "Features" toggles, pack half).

A user can hide a deployment-enabled pack's *surfaces* for themselves — its
situation tools and the ``/care`` lens — via a ``pack_enabled:<key>`` coaching
state of ``"off"``, without touching ``PREFRONTAL_PACKS``. This is the surfaces
overlay (P1): vocabulary/classification stay deployment-wide. Mirrors the
per-user module overlay.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.packs.registry import (
    user_disabled_pack_keys,
    user_enabled_situations,
    user_get_situation,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "pack-overlay-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, *, packs=("parent",)):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET, packs=packs))
    return TestClient(app)


def _auth():
    return {"X-Prefrontal-Token": SECRET}


# -- resolver ----------------------------------------------------------------


def test_user_disabling_pack_hides_its_situations(store):
    on = Settings(packs=("parent",))
    assert [t.key for t in user_enabled_situations(store, on)] == [
        "school_run", "pack_the_bag", "sick_day",
    ]
    store.set_state("pack_enabled:parent", "off", source="explicit")
    assert "parent" in user_disabled_pack_keys(store)
    assert user_enabled_situations(store, on) == []
    assert user_get_situation(store, "school_run", on) is None


# -- /packs/situations honors the overlay ------------------------------------


def test_situations_endpoint_empty_when_user_disables_pack(store):
    with _client(store, packs=("parent",)) as c:
        got = c.get("/packs/situations", headers=_auth())
        assert {t["tool"] for t in got.json()["situations"]} == {
            "school_run", "pack_the_bag", "sick_day",
        }
        # Turn the pack off for this user via the Features endpoint.
        r = c.post("/settings/features", json={"packs": {"parent": False}}, headers=_auth())
        assert r.status_code == 200
        assert {p["key"]: p["enabled"] for p in r.json()["packs"]}["parent"] is False
        # Its tools now disappear, and running one 404s (looks unknown).
        assert c.get("/packs/situations", headers=_auth()).json()["situations"] == []
        assert c.post("/packs/situations/school_run", headers=_auth()).status_code == 404


# -- /settings/features lists packs + round-trips ----------------------------


def test_features_lists_packs_and_toggles_back_on(store):
    with _client(store, packs=("parent",)) as c:
        got = c.get("/settings/features", headers=_auth())
        packs = {p["key"]: p for p in got.json()["packs"]}
        assert "parent" in packs and packs["parent"]["enabled"] is True
        assert {"key", "title", "description", "enabled"} <= set(packs["parent"])

        c.post("/settings/features", json={"packs": {"parent": False}}, headers=_auth())
        assert store.get_state("pack_enabled:parent") == "off"
        # Back on clears the override.
        c.post("/settings/features", json={"packs": {"parent": True}}, headers=_auth())
        assert store.get_state("pack_enabled:parent") is None
