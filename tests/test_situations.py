"""Pack situation tools — the read-only, on-demand questions a pack answers.

Covers the registry seam (only an enabled pack's tools are visible), the Parent
pack's school-run tool (composes the departure engine, narrowed to ``child``
commitments, read-only), and the ``/packs/situations`` router (list + run,
gated on the pack, unknown/disabled → 404).
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
from prefrontal.packs import enabled_situations, get_situation
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "situations-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, *, packs=()):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET, packs=packs))
    return TestClient(app)


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def _in(minutes: float) -> str:
    return (utcnow() + timedelta(minutes=minutes)).strftime(TS_FMT)


# -- registry ----------------------------------------------------------------


def test_parent_pack_declares_the_school_run_tool():
    from prefrontal.packs import get as get_pack

    parent = get_pack("parent")
    assert [t.key for t in parent.situations] == ["school_run"]


def test_situations_visible_only_when_owning_pack_enabled():
    # No pack: nothing to run.
    assert enabled_situations(Settings(packs=())) == []
    assert get_situation("school_run", Settings(packs=())) is None
    # Parent on: the school-run tool is exposed.
    on = Settings(packs=("parent",))
    assert [t.key for t in enabled_situations(on)] == ["school_run"]
    assert get_situation("school_run", on) is not None
    # An unknown key is None even with the pack on.
    assert get_situation("nope", on) is None


# -- the school-run handler --------------------------------------------------


def test_school_run_plans_child_departures_and_ignores_the_rest(store):
    # A kid's school event (child) — should plan; a self commitment — must not;
    # an fyi item — never attendable, so also excluded.
    kid_id, _ = store.upsert_commitment(title="School drop-off", start_at=_in(20))
    store.set_commitment_kind(kid_id, "child", "user")
    store.upsert_commitment(title="My standup", start_at=_in(20))  # self, excluded
    fyi_id, _ = store.upsert_commitment(title="Partner's dentist", start_at=_in(20))
    store.set_commitment_kind(fyi_id, "fyi", "user")

    tool = get_situation("school_run", Settings(packs=("parent",)))
    result = tool.handler(store)

    assert result["tool"] == "school_run"
    titles = [d["title"] for d in result["departures"]]
    assert titles == ["School drop-off"]
    only = result["departures"][0]
    assert only["commitment_id"] == kid_id
    assert only["leave_by"]  # a leave-by was computed
    # Soon enough to be reminder-worthy → a message is phrased and bubbles up as
    # the headline (the most-urgent leave-by, ready for a push).
    assert only["level"] != "none"
    assert only["message"]
    assert result["headline"] == only["message"]


def test_school_run_headline_empty_when_nothing_pressing(store):
    # A child commitment far out is planned but not reminder-worthy (level none),
    # so there is no headline to surface.
    far_id, _ = store.upsert_commitment(title="Sports day", start_at="2099-01-01 09:00:00")
    store.set_commitment_kind(far_id, "child", "user")

    tool = get_situation("school_run", Settings(packs=("parent",)))
    result = tool.handler(store)

    assert [d["title"] for d in result["departures"]] == ["Sports day"]
    assert result["departures"][0]["level"] == "none"
    assert result["headline"] == ""


# -- the /packs/situations router --------------------------------------------


def test_list_situations_reflects_enabled_pack(store):
    with _client(store) as c:  # no pack
        assert c.get("/packs/situations", headers=_auth()).json() == {"situations": []}
    with _client(store, packs=("parent",)) as c:
        body = c.get("/packs/situations", headers=_auth()).json()
    assert [s["tool"] for s in body["situations"]] == ["school_run"]
    assert body["situations"][0]["title"] == "School run"


def test_run_situation_returns_the_computed_result(store):
    kid_id, _ = store.upsert_commitment(title="Recital", start_at=_in(20))
    store.set_commitment_kind(kid_id, "child", "user")
    with _client(store, packs=("parent",)) as c:
        r = c.post("/packs/situations/school_run", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["tool"] == "school_run"
    assert [d["title"] for d in body["departures"]] == ["Recital"]


def test_run_unknown_tool_is_404(store):
    with _client(store, packs=("parent",)) as c:
        r = c.post("/packs/situations/nope", headers=_auth())
    assert r.status_code == 404


def test_run_tool_behind_disabled_pack_is_404(store):
    # The pack is off, so its tool is indistinguishable from one that doesn't exist.
    with _client(store) as c:
        r = c.post("/packs/situations/school_run", headers=_auth())
    assert r.status_code == 404
