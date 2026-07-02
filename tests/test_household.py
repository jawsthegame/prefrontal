"""Tests for the shared household sheet — repo scoping, render, assistant, HTTP.

Covers the four rollout layers of docs/household-sheet.md that ship v1: the
household-scoped store methods (two co-parents share rows, a non-member raises,
two households don't leak), the deterministic render, the plain-English
assistant ops, and the endpoints.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from prefrontal.assistant import build_snapshot, execute_actions, validate_actions
from prefrontal.config import Settings
from prefrontal.household import build_sheet, render_sheet
from prefrontal.memory.db import connect, init_db
from prefrontal.memory.migrate import backfill_added_columns
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def store():
    """In-memory store: operator + two co-parents (Dana, Alex) + a loner (Lee)."""
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "op", token="op-tok", is_operator=True)
    provision_user(s, "dana", display_name="Dana", token="dana-tok")
    provision_user(s, "alex", display_name="Alex", token="alex-tok")
    provision_user(s, "lee", display_name="Lee", token="lee-tok")
    hid = s.create_household("The Kims")
    s.set_user_household("dana", hid)
    s.set_user_household("alex", hid)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def dana(store):
    return store.scoped(store.get_user("dana")["id"])


@pytest.fixture()
def alex(store):
    return store.scoped(store.get_user("alex")["id"])


@pytest.fixture()
def lee(store):
    return store.scoped(store.get_user("lee")["id"])


# --- store scoping -----------------------------------------------------------


def test_co_parents_share_the_same_rows(store, dana, alex):
    """A fact Dana writes is visible to Alex — the whole point of household scope."""
    sam = dana.add_child(name="Sam")
    dana.set_fact(
        category="sizes",
        item="shoe size",
        value="13",
        updated_by=store.get_user("dana")["id"],
        child_id=sam,
    )
    facts = alex.facts()
    assert len(facts) == 1
    assert facts[0]["value"] == "13"
    assert facts[0]["child_name"] == "Sam"
    assert alex.children()[0]["name"] == "Sam"


def test_fact_upsert_is_in_place_and_restamps_provenance(store, dana, alex):
    """Re-setting a fact overwrites in place and re-attributes to the new author."""
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    dana.set_fact(category="sizes", item="shoe size", value="13", updated_by=dana_id)
    alex.set_fact(category="sizes", item="shoe size", value="1", updated_by=alex_id)
    facts = dana.facts()
    assert len(facts) == 1  # upsert, not a second row
    assert facts[0]["value"] == "1"
    assert facts[0]["updated_by_name"] == "Alex"


def test_non_member_raises_rather_than_reading_everyone(lee):
    """A user in no household gets a loud error, never a silent cross-household read."""
    with pytest.raises(RuntimeError):
        lee.facts()
    assert lee.household_id_or_none() is None


def test_two_households_do_not_leak(store, dana):
    """A second household's rows are invisible to the first."""
    other = store.create_household("Other")
    store.set_user_household("lee", other)
    lee = store.scoped(store.get_user("lee")["id"])
    lee.set_fact(
        category="food", item="allergy", value="dairy", updated_by=store.get_user("lee")["id"]
    )
    dana.set_fact(
        category="food", item="allergy", value="peanuts", updated_by=store.get_user("dana")["id"]
    )
    assert [f["value"] for f in dana.facts()] == ["peanuts"]
    assert [f["value"] for f in lee.facts()] == ["dairy"]


def test_clear_fact_and_remove_agreement(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_fact(category="health", item="dentist", value="Dr. Lin", updated_by=dana_id)
    aid = dana.set_agreement(title="Star chart", body="stars", kind="reward", updated_by=dana_id)
    assert dana.clear_fact(category="health", item="dentist") is True
    assert dana.clear_fact(category="health", item="dentist") is False  # already gone
    assert dana.remove_agreement(aid) is True
    assert dana.agreements() == []


def test_add_child_is_idempotent_on_name(dana):
    first = dana.add_child(name="Sam")
    again = dana.add_child(name="Sam", birthday="2016-05-02")
    assert first == again  # same row, not a duplicate
    assert len(dana.children()) == 1


def test_set_user_household_rejects_unknown_household(store):
    with pytest.raises(ValueError):
        store.set_user_household("dana", 999)


# --- render ------------------------------------------------------------------


def test_render_sections_and_provenance(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    sam = dana.add_child(name="Sam")
    dana.set_fact(category="sizes", item="shoe size", value="13", updated_by=dana_id, child_id=sam)
    alex.set_fact(
        category="food", item="allergy", value="peanuts — EpiPen", updated_by=alex_id, child_id=sam
    )
    dana.set_agreement(
        title="Star chart",
        body="Stars for the morning routine.",
        kind="reward",
        updated_by=dana_id,
        child_id=sam,
        structured=(
            '{"unit":"star","earn_only":true,'
            '"thresholds":[{"stars":10,"reward":"small Lego"}]}'
        ),
    )
    text = render_sheet(build_sheet(dana, now=NOW))
    assert "The Kims — shared sheet" in text
    assert "Recently changed" in text
    assert "Sam" in text and "shoe size: 13" in text
    assert "peanuts — EpiPen" in text
    assert "10 stars → small Lego" in text
    assert "earn-only" in text
    # provenance shows in the recently-changed surface
    assert "Dana" in text and "Alex" in text


def test_render_empty_sheet_is_a_gentle_prompt(dana):
    text = render_sheet(build_sheet(dana, now=NOW))
    assert "Nothing here yet" in text


def test_upcoming_appointments_from_child_commitments(store, alex):
    """A kind='child' commitment on a parent's calendar surfaces on the sheet."""
    alex.add_child(name="Sam")
    alex.upsert_commitment(
        title="Sam dentist",
        start_at="2026-07-07 15:00:00",
        location="Dr. Lin",
        source="manual",
        kind="child",
        kind_source="user",
    )
    sheet = build_sheet(alex, now=NOW)
    assert sheet.counts["upcoming"] == 1
    assert sheet.upcoming[0].title == "Sam dentist"
    assert "Upcoming appointments" in render_sheet(sheet)


# --- assistant ops -----------------------------------------------------------


def test_snapshot_includes_household_for_member_only(dana, lee):
    dana.add_child(name="Sam")
    snap = build_snapshot(dana)
    assert "household" in snap
    assert snap["household"]["children"][0]["name"] == "Sam"
    assert "sizes" in snap["household"]["fact_categories"]
    # A non-member's snapshot omits household entirely.
    assert "household" not in build_snapshot(lee)


def test_assistant_sets_and_clears_facts_with_attribution(store, dana):
    sam = dana.add_child(name="Sam")
    snap = build_snapshot(dana)
    actions, errors = validate_actions(
        [
            {
                "op": "set_fact",
                "category": "sizes",
                "item": "Shoe Size",
                "value": "13",
                "child": sam,
            },
            {"op": "set_fact", "category": "health", "item": "dentist", "value": "Dr. Lin"},
            {"op": "set_fact", "category": "bogus", "item": "x", "value": "y"},
        ],
        snap,
    )
    assert len(actions) == 2  # the bad-category one is dropped
    assert any("category must be one of" in e for e in errors)
    results = execute_actions(dana, actions)
    assert all(r["ok"] for r in results)
    facts = {f["item"]: f for f in dana.facts()}
    assert facts["shoe size"]["value"] == "13"  # item normalized
    assert facts["shoe size"]["updated_by_name"] == "Dana"  # acting user, never model-supplied


def test_assistant_agreement_round_trip(dana):
    sam = dana.add_child(name="Sam")
    snap = build_snapshot(dana)
    actions, _ = validate_actions(
        [
            {
                "op": "set_agreement",
                "title": "Star chart",
                "body": "stars",
                "kind": "reward",
                "child": sam,
                "structured": {"unit": "star", "thresholds": [{"stars": 10, "reward": "lego"}]},
            }
        ],
        snap,
    )
    execute_actions(dana, actions)
    assert dana.agreements()[0]["title"] == "Star chart"
    # remove it by the id the fresh snapshot exposes
    snap2 = build_snapshot(dana)
    aid = snap2["household"]["agreements"][0]["id"]
    acts2, _ = validate_actions([{"op": "remove_agreement", "agreement_id": aid}], snap2)
    execute_actions(dana, acts2)
    assert dana.agreements() == []


def test_household_ops_rejected_for_non_member(lee):
    snap = build_snapshot(lee)
    actions, errors = validate_actions(
        [{"op": "set_fact", "category": "sizes", "item": "x", "value": "y"}], snap
    )
    assert actions == []
    assert errors and "household" in errors[0]


def test_assistant_rejects_unknown_child_and_agreement(dana):
    dana.add_child(name="Sam")
    snap = build_snapshot(dana)
    actions, errors = validate_actions(
        [
            {"op": "set_fact", "category": "sizes", "item": "x", "value": "y", "child": 999},
            {"op": "remove_agreement", "agreement_id": 999},
        ],
        snap,
    )
    assert actions == []
    assert len(errors) == 2


# --- endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        yield c


def _h(token):
    return {"X-Prefrontal-Token": token}


def test_sheet_endpoint_is_shared_across_co_parents(client):
    # Dana writes via the assistant apply path; Alex reads the same sheet.
    apply = client.post(
        "/assistant/apply",
        json={
            "actions": [{"op": "set_fact", "category": "sizes", "item": "shoe size", "value": "13"}]
        },
        headers=_h("dana-tok"),
    )
    assert apply.status_code == 200 and apply.json()["applied"] == 1
    resp = client.get("/household/sheet", headers=_h("alex-tok"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["sheet"]["counts"]["facts"] == 1
    assert "shoe size" in body["markdown"]
    assert body["sheet"]["recently_changed"][0]["who"] == "Dana"


def test_sheet_endpoint_404_for_non_member(client):
    assert client.get("/household/sheet", headers=_h("lee-tok")).status_code == 404


def test_operator_creates_household_and_adds_member(client):
    created = client.post("/admin/households", json={"name": "New Home"}, headers=_h("op-tok"))
    assert created.status_code == 201
    hid = created.json()["id"]
    added = client.post(
        f"/admin/households/{hid}/members", json={"handle": "lee"}, headers=_h("op-tok")
    )
    assert added.status_code == 200
    assert any(m["handle"] == "lee" for m in added.json()["members"])


def test_operator_endpoints_reject_non_operator_and_bad_ids(client):
    assert (
        client.post("/admin/households", json={"name": "x"}, headers=_h("dana-tok")).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/households/999/members", json={"handle": "dana"}, headers=_h("op-tok")
        ).status_code
        == 404
    )


# --- migration ---------------------------------------------------------------


def test_household_id_column_backfilled_on_old_db(tmp_path):
    """A users table predating household_id gains the column via the back-fill."""
    db = str(tmp_path / "old.db")
    conn = connect(db)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT, "
        "token_hash TEXT, status TEXT DEFAULT 'active', is_operator BOOLEAN DEFAULT 0)"
    )
    conn.commit()
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    assert "household_id" in cols
    conn.close()
