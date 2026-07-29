"""A disabled module's *surfaces* go with it, not just its nudges.

The nudge half landed first (see ``tests/test_disabled_enforcement.py``): with a
module off, no fire path acts. This is the pull half — the cards, lists and
controls that only exist because that module runs. Each read reports its normal
"nothing here" shape plus ``module_off`` (so an older client degrades to empty
instead of breaking, and an updated one can name the switch), and each write that
would only feed the intervention is refused with 409.

Two deliberate exemptions are pinned here too, because they're judgment calls a
future change shouldn't quietly undo:

* **capture stays open** — parking an impulse writes a real todo; refusing it
  would lose what the user typed.
* **emotion support stays open** — it screens for crisis language first, so a 409
  would replace crisis resources with an error in the worst possible moment.
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
from prefrontal.modules.registry import MODULE_ENABLED_PREFIX
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "surfaces-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET, modules=(), packs=()))
    c = TestClient(app)
    c.__enter__()
    return c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _off(store, key: str) -> None:
    store.set_state(f"{MODULE_ENABLED_PREFIX}{key}", "off", source="explicit")


# -- reads report module_off with an empty (not broken) payload ---------------

READS = (
    ("self_care", "/self-care", "checks"),
    ("self_care", "/self-care/review", "gaps"),
    ("hyperfocus", "/focus", "active"),
    ("location_anchor", "/outings", "active"),
    ("impulsivity", "/impulses/parked", "parked"),
    ("trip_tracking", "/trips", "recent"),
)


@pytest.mark.parametrize(("key", "path", "list_field"), READS)
def test_read_surface_is_live_while_the_module_is_on(client, key, path, list_field):
    body = client.get(path, headers=_auth()).json()
    assert body.get("module_off") is None, f"{path} claims off with {key} enabled"
    assert list_field in body


@pytest.mark.parametrize(("key", "path", "list_field"), READS)
def test_read_surface_reports_module_off_and_empties(client, store, key, path, list_field):
    _off(store, key)
    r = client.get(path, headers=_auth())
    assert r.status_code == 200, f"{path} should stay a 200 (empty), not an error"
    body = r.json()
    assert body["module_off"] is True
    # The shape a client already renders for "nothing here" — so an older build
    # that ignores module_off shows an empty card rather than failing to decode.
    assert body[list_field] == []


def test_self_care_off_reads_as_master_off_for_older_clients(client, store):
    """`enabled: false` keeps pre-`module_off` clients (and the ring) correct."""
    _off(store, "self_care")
    body = client.get("/self-care", headers=_auth()).json()
    assert body["enabled"] is False
    # ``null``, not ``{}``: the settings card renders the evening-review row only
    # when the block is present, so an empty dict would draw a blank row.
    assert body["review"] is None


def test_trips_off_keeps_the_label_vocabularies(client, store):
    """The form vocabularies are static config, so they stay — only data empties."""
    _off(store, "trip_tracking")
    body = client.get("/trips", headers=_auth()).json()
    assert body["categories"] and body["domains"]
    assert body["active"] is None


def test_read_surface_reports_module_off_deployment_wide(store):
    """Deployment-off looks identical to per-user off on these surfaces."""
    app = create_app(
        store=store, settings=Settings(webhook_secret=SECRET, modules=("projects",), packs=())
    )
    c = TestClient(app)
    c.__enter__()
    assert c.get("/focus", headers=_auth()).json()["module_off"] is True
    assert c.get("/trips", headers=_auth()).json()["module_off"] is True


# -- writes that only feed the intervention are refused ----------------------


def test_starting_a_focus_session_is_refused_when_hyperfocus_is_off(client, store):
    _off(store, "hyperfocus")
    r = client.post("/webhooks/focus/start", headers=_auth(), json={"intended_task": "deep work"})
    assert r.status_code == 409
    assert "Settings ▸ Features" in r.json()["detail"]
    assert store.active_focus_sessions() == []


def test_starting_an_outing_is_refused_when_the_anchor_is_off(client, store):
    _off(store, "location_anchor")
    r = client.post(
        "/webhooks/outing/start",
        headers=_auth(),
        json={"intention": "coffee", "time_window_minutes": 20},
    )
    assert r.status_code == 409
    assert store.active_outings() == []


def test_closing_an_open_outing_still_works_after_switching_off(client, store):
    """A mid-outing toggle must not strand the open outing."""
    started = client.post(
        "/webhooks/outing/start",
        headers=_auth(),
        json={"intention": "coffee", "time_window_minutes": 20},
    )
    assert started.status_code == 201
    _off(store, "location_anchor")
    assert client.post("/webhooks/outing/return", headers=_auth(), json={}).status_code == 200


def test_self_care_writes_are_refused_when_the_module_is_off(client, store):
    _off(store, "self_care")
    assert client.post("/self-care", headers=_auth(), json={"enabled": True}).status_code == 409
    assert (
        client.post("/self-care/mark", headers=_auth(), json={"key": "water"}).status_code == 409
    )


# -- the deliberate exemptions ----------------------------------------------


def test_capturing_an_impulse_still_works_with_impulsivity_off(client, store):
    """Capture writes a real todo — refusing it would lose the user's text."""
    _off(store, "impulsivity")
    r = client.post(
        "/webhooks/impulse/capture", headers=_auth(), json={"impulse_text": "buy a drone"}
    )
    assert r.status_code == 201
    assert any("drone" in (t["title"] or "").lower() for t in store.open_todos())


def test_emotion_support_still_answers_with_the_module_off(client, store):
    """Crisis screening runs here; a 409 in a hard moment is not acceptable."""
    _off(store, "emotion_regulation")
    r = client.post("/emotion/support", headers=_auth(), json={"text": "I'm overwhelmed"})
    assert r.status_code == 200
    assert r.json()["text"]


def test_projects_stay_visible_with_the_staleness_module_off(client, store):
    """The `projects` module is only a staleness nudge; projects are a core feature."""
    created = client.post(
        "/projects", headers=_auth(), json={"name": "Kitchen", "domain": "home"}
    )
    assert created.status_code == 201
    _off(store, "projects")
    body = client.get("/projects", headers=_auth()).json()
    assert [p["name"] for p in body["projects"]] == ["Kitchen"]
    assert body.get("module_off") is None


def test_location_pings_still_feed_trip_detection_with_trip_tracking_off(client, store):
    """Passive detection carries vacation-mode auto-lift; it must not be gated."""
    _off(store, "trip_tracking")
    r = client.post("/webhooks/location", headers=_auth(), json={"lat": 47.6, "lon": -122.3})
    assert r.status_code == 200
    assert r.json()["stored"] is True


# -- nothing is destroyed: flipping back on restores the surface -------------


def test_switching_back_on_restores_the_hidden_history(client, store):
    now = utcnow()
    store.upsert_commitment(
        title="Focus: design doc",
        start_at=(now - timedelta(minutes=5)).strftime(TS_FMT),
        end_at=(now + timedelta(minutes=55)).strftime(TS_FMT),
    )
    assert client.post("/webhooks/focus/arm", headers=_auth(), json={}).json()["armed"] is True
    _off(store, "hyperfocus")
    assert client.get("/focus", headers=_auth()).json()["active"] == []
    store.delete_state(f"{MODULE_ENABLED_PREFIX}hyperfocus")
    back = client.get("/focus", headers=_auth()).json()
    assert len(back["active"]) == 1
    assert back.get("module_off") is None
