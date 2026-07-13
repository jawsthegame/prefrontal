"""The caregiver ``/care/sheet`` surface (Context Pack "surface tailoring", slice 2).

Gated on the Caregiver pack (not household membership), it surfaces upcoming
``kind='care'`` appointments and open todos in the pack's caregiver categories.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "care-secret"


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


def test_care_sheet_disabled_when_pack_off(store):
    # Without the caregiver pack, the surface reports disabled with empty lists so
    # the page can prompt to enable it — no 404.
    with _client(store) as c:
        body = c.get("/care/sheet", headers=_auth()).json()
    assert body == {"enabled": False, "appointments": [], "todos": []}


def test_care_sheet_surfaces_care_appointments_and_care_todos(store):
    # A care-recipient appointment (shows), a plain self appointment (must not),
    # a caregiver-category todo (shows), and a non-caregiver todo (must not).
    care_id, _ = store.upsert_commitment(
        title="Mom's cardiology", start_at="2099-01-01 10:00:00"
    )
    store.set_commitment_kind(care_id, "care", "user")
    store.upsert_commitment(title="My dentist", start_at="2099-01-01 12:00:00")
    store.add_todo("Refill Mom's prescription", category="medical")
    store.add_todo("Buy groceries", category="errand")  # not a caregiver category

    with _client(store, packs=("caregiver",)) as c:
        body = c.get("/care/sheet", headers=_auth()).json()

    assert body["enabled"] is True
    assert [a["title"] for a in body["appointments"]] == ["Mom's cardiology"]
    todo_titles = [t["title"] for t in body["todos"]]
    assert "Refill Mom's prescription" in todo_titles
    assert "Buy groceries" not in todo_titles


def test_care_recipients_disabled_when_pack_off(store):
    with _client(store) as c:
        body = c.get("/care/recipients", headers=_auth()).json()
        assert body == {"enabled": False, "names": []}
        # A write is refused when the pack is off.
        r = c.post("/care/recipients", headers=_auth(), json={"names": ["Mom"]})
    assert r.status_code == 409


def test_care_recipients_set_get_and_normalization(store):
    with _client(store, packs=("caregiver",)) as c:
        # Empty roster to start.
        assert c.get("/care/recipients", headers=_auth()).json() == {
            "enabled": True,
            "names": [],
        }
        # A write normalizes (trim, de-dupe) and echoes the stored list.
        body = c.post(
            "/care/recipients", headers=_auth(), json={"names": ["  Mom ", "Dad", "mom"]}
        ).json()
        assert body == {"enabled": True, "names": ["Mom", "Dad"]}
        assert c.get("/care/recipients", headers=_auth()).json()["names"] == ["Mom", "Dad"]
        # An empty list clears the roster.
        assert c.post("/care/recipients", headers=_auth(), json={"names": []}).json()[
            "names"
        ] == []


def test_care_recipient_names_drive_care_classification(store):
    # The roster tags a matching synced event 'care' deterministically (offline),
    # so it then shows on the care sheet — the whole point of the roster.
    store.set_care_recipient_names(["Mom"])
    with _client(store, packs=("caregiver",)) as c:
        c.post(
            "/webhooks/calendar/sync",
            headers=_auth(),
            json={
                "events": [
                    {
                        "title": "Mom cardiology",
                        "start_at": "2099-02-02 10:00:00",
                        "external_id": "ev-mom-1",
                    }
                ]
            },
        )
        body = c.get("/care/sheet", headers=_auth()).json()
    assert [a["title"] for a in body["appointments"]] == ["Mom cardiology"]


def test_care_page_serves_html(store):
    # The /care page is a self-contained shell (data-less); it loads regardless of
    # the pack (the data endpoint does the gating).
    with _client(store) as c:
        r = c.get("/care")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/care/sheet" in r.text  # the page fetches the data endpoint
