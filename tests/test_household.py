"""Tests for the shared household sheet — repo scoping, render, assistant, HTTP.

Covers the four rollout layers of docs/household-sheet.md that ship v1: the
household-scoped store methods (two co-parents share rows, a non-member raises,
two households don't leak), the deterministic render, the plain-English
assistant ops, and the endpoints.
"""

from __future__ import annotations

import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.assistant import build_snapshot, execute_actions, validate_actions
from prefrontal.config import Settings
from prefrontal.household import (
    balance_view,
    build_sheet,
    checkin_due,
    checkin_summary,
    digest_interval_ok,
    digest_message,
    newly_reached_goals,
    next_goal,
    normalize_checkin_config,
    normalize_prompt,
    parse_structured,
    prompt_due,
    prompt_question,
    render_sheet,
    star_congrats_text,
    unseen_changes,
    week_key,
)
from prefrontal.impact import utcnow
from prefrontal.integrations.delivery import (
    DeliveryClient,
    deliver_to_household,
    deliver_to_member,
    household_checkin_notice,
    household_digest_notice,
    household_notice,
    household_prompt_notice,
)
from prefrontal.memory.db import connect, init_db
from prefrontal.memory.migrate import backfill_added_columns
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.scheduling import local_datetime
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.oauth import sign_action, verify_action

BASE = "https://agent-1.tail8b0a.ts.net"
SIGNING = "household-signing-key"

STAR_CHART = {
    "unit": "star",
    "earn_only": True,
    "thresholds": [
        {"stars": 5, "reward": "movie night"},
        {"stars": 10, "reward": "small Lego set"},
    ],
}

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
            '{"unit":"star","earn_only":true,"thresholds":[{"stars":10,"reward":"small Lego"}]}'
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


def test_per_child_association_survives_wire_round_trip(dana):
    """The /assistant → preview → /assistant/apply echo must not drop the child.

    The dashboard echoes the wire actions (``to_wire()``, which emits ``child_id``)
    back to /assistant/apply verbatim, where they are re-validated. Regression for
    the ``_resolve_child`` asymmetry that read ``child`` but wrote ``child_id`` —
    the second validation silently defaulted every per-child fact/agreement to the
    household (child_id 0) despite a preview that said otherwise.
    """
    sam = dana.add_child(name="Sam")
    snap = build_snapshot(dana)
    # First pass: the model's raw op uses the ``child`` key.
    planned, _ = validate_actions(
        [
            {"op": "set_fact", "category": "sizes", "item": "shoe size",
             "value": "13", "child": sam},
            {"op": "set_agreement", "title": "Star chart", "kind": "reward",
             "body": "stars", "child": sam},
        ],
        snap,
    )
    assert all(a.params["child_id"] == sam for a in planned)
    # Echo the wire form back through validation, exactly as the client does.
    wire = [a.to_wire() for a in planned]
    assert all("child_id" in w and "child" not in w for w in wire)
    reapplied, errors = validate_actions(wire, snap)
    assert not errors
    assert [a.params["child_id"] for a in reapplied] == [sam, sam]
    execute_actions(dana, reapplied)
    # The fact and the agreement land on Sam, not the household.
    assert dana.facts()[0]["child_id"] == sam
    assert dana.agreements()[0]["child_id"] == sam


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


# --- write endpoints (the /kids dashboard's inline forms) --------------------


def test_write_endpoints_full_flow_and_sharing(client):
    """Direct-form writes by one parent are visible on the other's sheet."""
    # roster
    r = client.post(
        "/household/children",
        json={"name": "Sam", "birthday": "2016-05-02"},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 201
    sam = r.json()["id"]
    assert (
        client.post(
            f"/household/children/{sam}", json={"name": "Samuel"}, headers=_h("alex-tok")
        ).status_code
        == 200
    )
    # facts (Dana writes, Alex sees + clears)
    assert (
        client.post(
            "/household/facts",
            json={"child_id": sam, "category": "sizes", "item": "Shoe Size", "value": "13"},
            headers=_h("dana-tok"),
        ).status_code
        == 200
    )
    sheet = client.get("/household/sheet", headers=_h("alex-tok")).json()["sheet"]
    assert sheet["counts"]["facts"] == 1
    assert (
        client.post(
            "/household/facts/clear",
            json={"child_id": sam, "category": "sizes", "item": "shoe size"},
            headers=_h("alex-tok"),
        ).json()["removed"]
        is True
    )
    # agreement with a star chart, then remove
    r = client.post(
        "/household/agreements",
        json={
            "child_id": sam,
            "title": "Star chart",
            "kind": "reward",
            "body": "stars",
            "structured": {"unit": "star", "thresholds": [{"stars": 10, "reward": "lego"}]},
        },
        headers=_h("dana-tok"),
    )
    assert r.status_code == 200
    aid = r.json()["id"]
    assert (
        client.post(f"/household/agreements/{aid}/remove", json={}, headers=_h("alex-tok")).json()[
            "removed"
        ]
        is True
    )
    # appointment (kind='child' commitment on the caller's calendar)
    r = client.post(
        "/household/appointments",
        json={"title": "Sam dentist", "start_at": "2026-07-07 15:00", "location": "Dr Lin"},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 201 and r.json()["created"] is True


def test_write_endpoints_validate(client):
    assert (
        client.post(
            "/household/facts",
            json={"category": "nope", "item": "x", "value": "y"},
            headers=_h("dana-tok"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/household/agreements", json={"title": "P", "kind": "bogus"}, headers=_h("dana-tok")
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/household/appointments",
            json={"title": "x", "start_at": "not-a-date"},
            headers=_h("dana-tok"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/household/agreements/{999}/remove", json={}, headers=_h("dana-tok")
        ).status_code
        == 404
    )


def test_write_endpoints_are_member_guarded(client):
    """A user in no household gets 404 from every write route, not a 500."""
    for path, body in [
        ("/household/children", {"name": "X"}),
        ("/household/facts", {"category": "sizes", "item": "x", "value": "y"}),
        ("/household/facts/clear", {"category": "sizes", "item": "x"}),
        ("/household/agreements", {"title": "P"}),
        ("/household/appointments", {"title": "x", "start_at": "2026-07-07 15:00"}),
    ]:
        assert client.post(path, json=body, headers=_h("lee-tok")).status_code == 404


def test_kids_page_and_family_link_serve(client):
    assert client.get("/kids").status_code == 200
    assert "/kids" in client.get("/family").text


# --- star tracking: pure goal logic ------------------------------------------


def test_newly_reached_goals_only_fires_on_crossing():
    """A goal reports once — when the total carries across it, not on every award."""
    # 4 → 6 crosses the 5-star goal (and only that one).
    reached = newly_reached_goals(STAR_CHART, before=4, after=6)
    assert [g["count"] for g in reached] == [5]
    assert reached[0]["reward"] == "movie night"
    # Already past it → nothing new.
    assert newly_reached_goals(STAR_CHART, before=6, after=8) == []
    # A single leap can clear several goals at once (each is its own reward).
    assert [g["count"] for g in newly_reached_goals(STAR_CHART, 0, 12)] == [5, 10]


def test_next_goal_and_congrats_text():
    assert next_goal(STAR_CHART, 0)["count"] == 5
    nxt = next_goal(STAR_CHART, 7)
    assert nxt["count"] == 10 and nxt["remaining"] == 3
    assert next_goal(STAR_CHART, 10) is None  # all rewards earned
    goal = newly_reached_goals(STAR_CHART, 4, 5)[0]
    assert star_congrats_text("Sam", goal) == "🌟 Sam hit 5 stars — reward unlocked: movie night!"


def test_goal_logic_tolerates_no_or_bad_structure():
    assert newly_reached_goals(None, 0, 100) == []
    assert next_goal(parse_structured(None), 3) is None
    assert next_goal(parse_structured('{"unit":"point","thresholds":[{"points":3,"reward":"x"}]}'),
                     1)["reward"] == "x"


# --- star tracking: repo -----------------------------------------------------


def _star_agreement(scoped, store, *, child_id=0):
    return scoped.set_agreement(
        title="Star chart", body="stars", kind="reward",
        updated_by=store.get_user("dana")["id"], child_id=child_id,
        structured=__import__("json").dumps(STAR_CHART),
    )


def test_award_stars_accumulates_and_reports_totals(store, dana, alex):
    """Grants append to a shared ledger; the total is visible to both parents."""
    aid = _star_agreement(dana, store)
    dana_id = store.get_user("dana")["id"]
    r1 = dana.award_stars(agreement_id=aid, delta=3, awarded_by=dana_id)
    assert (r1["before"], r1["after"]) == (0, 3)
    r2 = alex.award_stars(agreement_id=aid, delta=2, awarded_by=store.get_user("alex")["id"])
    assert (r2["before"], r2["after"]) == (3, 5)
    # Either parent sees the same running total (household-scoped, not per-user).
    assert dana.star_total(aid) == 5 == alex.star_total(aid)
    assert alex.star_totals() == {aid: 5}


def test_award_stars_rejects_foreign_agreement(store, dana):
    """Awarding against another household's agreement is a None (→ 404), not a write."""
    other = store.create_household("Other")
    store.set_user_household("lee", other)
    lee = store.scoped(store.get_user("lee")["id"])
    aid = _star_agreement(dana, store)
    assert lee.award_stars(agreement_id=aid, delta=1, awarded_by=None) is None
    assert dana.star_total(aid) == 0  # nothing leaked in


def test_star_ledger_and_recent_awards_carry_provenance(store, dana, alex):
    sam = dana.add_child(name="Sam")
    aid = _star_agreement(dana, store, child_id=sam)
    dana.award_stars(agreement_id=aid, delta=1, awarded_by=store.get_user("dana")["id"],
                     note="made bed")
    alex.award_stars(agreement_id=aid, delta=2, awarded_by=store.get_user("alex")["id"])
    ledger = dana.star_ledger(aid)
    assert [row["awarded_by_name"] for row in ledger] == ["Alex", "Dana"]  # newest first
    recent = alex.recent_star_awards()
    assert recent[0]["child_name"] == "Sam"
    assert recent[0]["agreement_title"] == "Star chart"


def test_star_progress_and_grants_render_on_the_sheet(store, dana):
    sam = dana.add_child(name="Sam")
    aid = _star_agreement(dana, store, child_id=sam)
    dana.award_stars(agreement_id=aid, delta=7, awarded_by=store.get_user("dana")["id"])
    text = render_sheet(build_sheet(dana, now=NOW))
    assert "7 stars so far" in text
    assert "3 to go → small Lego set" in text  # next unreached goal
    assert "Sam · +7⭐ (Star chart)" in text     # the grant on the load surface


# --- star tracking: notify both parents --------------------------------------


def test_deliver_to_household_reaches_every_member(store):
    """One notice fans out to both co-parents on their own ntfy topics."""
    dana = store.scoped(store.get_user("dana")["id"])
    alex = store.scoped(store.get_user("alex")["id"])
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.read())["topic"])
        return httpx.Response(200, json={"id": "x"})

    client = DeliveryClient.from_settings(Settings(), transport=httpx.MockTransport(handler))
    hid = store.get_user("dana")["household_id"]
    results = deliver_to_household(
        store, hid, household_notice("🌟 goal!"), settings=Settings(), client=client
    )
    assert sorted(sent) == ["alex-topic", "dana-topic"]
    assert all(r["delivered"] for r in results)
    assert {r["handle"] for r in results} == {"dana", "alex"}


# --- star tracking: endpoint -------------------------------------------------


def test_award_endpoint_hits_goal_and_notifies_both_parents(client):
    """Crossing a reward threshold returns the goal and notifies every member."""
    r = client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "body": "stars", "structured": STAR_CHART},
        headers=_h("dana-tok"),
    )
    aid = r.json()["id"]
    # 4 stars — below the first (5-star) goal: nothing fires yet.
    first = client.post(
        f"/household/agreements/{aid}/stars", json={"delta": 4}, headers=_h("dana-tok")
    ).json()
    assert first["total"] == 4 and first["goals_reached"] == []
    assert first["next_goal"]["count"] == 5
    # One more crosses it → goal reached, both parents notified.
    hit = client.post(
        f"/household/agreements/{aid}/stars",
        json={"delta": 1, "note": "cleared the table"},
        headers=_h("alex-tok"),
    ).json()
    assert hit["total"] == 5
    assert [g["reward"] for g in hit["goals_reached"]] == ["movie night"]
    assert {n["handle"] for n in hit["notified"]} == {"dana", "alex"}
    assert hit["next_goal"]["reward"] == "small Lego set"


def test_award_endpoint_earn_only_rejects_taking_stars_away(client):
    r = client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "structured": STAR_CHART},
        headers=_h("dana-tok"),
    )
    aid = r.json()["id"]
    assert (
        client.post(
            f"/household/agreements/{aid}/stars", json={"delta": -1}, headers=_h("dana-tok")
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/household/agreements/{aid}/stars", json={"delta": 0}, headers=_h("dana-tok")
        ).status_code
        == 422
    )


def test_award_endpoint_unknown_agreement_and_non_member(client):
    assert (
        client.post(
            "/household/agreements/999/stars", json={"delta": 1}, headers=_h("dana-tok")
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/household/agreements/1/stars", json={"delta": 1}, headers=_h("lee-tok")
        ).status_code
        == 404
    )


# --- scheduled award prompts: pure logic -------------------------------------


def test_normalize_prompt_validates_and_cleans():
    clean, err = normalize_prompt(
        {"enabled": True, "days": [2, 2, 9, 0], "time": "7:5", "question": "  Did   Sam?  "}
    )
    assert err is None
    assert clean["days"] == [0, 2]  # de-duped, out-of-range 9 dropped, sorted
    assert clean["time"] == "07:05"
    assert clean["question"] == "Did Sam?"  # whitespace collapsed
    # an enabled schedule with no days would silently never fire → rejected
    assert normalize_prompt({"enabled": True, "days": [], "time": "19:30"})[1]
    assert normalize_prompt({"time": "nope"})[1]
    # disabled with no days is fine (turning reminders off)
    c2, e2 = normalize_prompt({"enabled": False, "days": [], "time": "19:30"})
    assert e2 is None and c2["enabled"] is False


def test_prompt_due_weekday_time_and_daily_dedup():
    struct = {"prompt": {"enabled": True, "days": [0], "time": "19:30"}}  # Mondays 7:30pm
    mon_8pm = datetime.datetime(2026, 7, 6, 20, 0)  # 2026-07-06 is a Monday
    assert prompt_due(struct, now_local=mon_8pm) is True
    too_early = datetime.datetime(2026, 7, 6, 19, 0)
    assert prompt_due(struct, now_local=too_early) is False  # before the time
    assert prompt_due(struct, now_local=datetime.datetime(2026, 7, 7, 20, 0)) is False  # not Mon
    # already asked today → held for the rest of the day
    already = datetime.datetime(2026, 7, 6, 19, 31)
    assert prompt_due(struct, now_local=mon_8pm, last_prompted_local=already) is False
    # disabled never fires
    off = {"prompt": {"enabled": False, "days": [0], "time": "19:30"}}
    assert prompt_due(off, now_local=mon_8pm) is False


def test_prompt_question_custom_and_default():
    assert prompt_question({"prompt": {"question": "Bed made?"}}, "Sam", "Chart") == "Bed made?"
    default = prompt_question({}, "Sam", "Morning routine")
    assert default == "🌟 Did Sam earn a star for Morning routine today?"
    assert "the kids" in prompt_question({}, None, "Chores")


# --- scheduled award prompts: repo + endpoints -------------------------------


def test_mark_prompted_stamps_and_surfaces(store, dana):
    aid = _star_agreement(dana, store)
    assert dana.agreement(aid)["last_prompted_at"] is None
    assert dana.mark_prompted(aid) is True
    assert dana.agreement(aid)["last_prompted_at"] is not None
    assert dana.agreements()[0]["last_prompted_at"] is not None


def test_set_prompt_endpoint_merges_and_validates(client):
    aid = client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "structured": STAR_CHART},
        headers=_h("dana-tok"),
    ).json()["id"]
    ok = client.post(
        f"/household/agreements/{aid}/prompt",
        json={"enabled": True, "days": [0, 1, 2, 3, 4], "time": "19:30", "question": "Bed?"},
        headers=_h("dana-tok"),
    )
    assert ok.status_code == 200 and ok.json()["prompt"]["days"] == [0, 1, 2, 3, 4]
    # thresholds are preserved alongside the new prompt, visible to the other parent
    agr = client.get("/household/sheet", headers=_h("alex-tok")).json()["sheet"]["agreements"][0]
    assert agr["structured"]["prompt"]["time"] == "19:30"
    assert len(agr["structured"]["thresholds"]) == 2
    # bad time → 422; unknown agreement → 404
    assert client.post(
        f"/household/agreements/{aid}/prompt",
        json={"enabled": True, "days": [0], "time": "nope"}, headers=_h("dana-tok"),
    ).status_code == 422
    assert client.post(
        "/household/agreements/999/prompt",
        json={"enabled": True, "days": [0], "time": "19:30"}, headers=_h("dana-tok"),
    ).status_code == 404


def test_star_prompt_check_sends_once_per_day_to_both_parents(client):
    aid = client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "structured": STAR_CHART},
        headers=_h("dana-tok"),
    ).json()["id"]
    # daily at 00:00 → due on any check today, regardless of timezone/day
    client.post(
        f"/household/agreements/{aid}/prompt",
        json={"enabled": True, "days": [0, 1, 2, 3, 4, 5, 6], "time": "00:00"},
        headers=_h("dana-tok"),
    )
    check = "/webhooks/household/star-prompts/check"
    first = client.post(check, json={}, headers=_h("alex-tok")).json()
    assert len(first["sent"]) == 1
    assert {n["handle"] for n in first["sent"][0]["notified"]} == {"dana", "alex"}
    # a second sweep the same day is a no-op (last_prompted_at dedups)
    second = client.post(check, json={}, headers=_h("dana-tok")).json()
    assert second["sent"] == []


def test_star_prompt_check_skips_charts_without_a_schedule(client):
    client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "structured": STAR_CHART},
        headers=_h("dana-tok"),
    )
    assert client.post(
        "/webhooks/household/star-prompts/check", json={}, headers=_h("dana-tok")
    ).json()["sent"] == []
    assert client.post(
        "/webhooks/household/star-prompts/check", json={}, headers=_h("lee-tok")
    ).status_code == 404  # non-member


# --- scheduled award prompts: one-tap buttons + tap --------------------------


def test_prompt_notice_carries_signed_star_buttons(store):
    """Each parent's prompt push gets ⭐ Yes / Not today, signed for that parent."""
    store.scoped(store.get_user("dana")["id"]).set_state("ntfy_topic", "dana-topic")
    store.scoped(store.get_user("alex")["id"]).set_state("ntfy_topic", "alex-topic")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={"id": "x"})

    client = DeliveryClient.from_settings(Settings(), transport=httpx.MockTransport(handler))
    hid = store.get_user("dana")["household_id"]
    deliver_to_household(
        store, hid, household_prompt_notice("Did Sam earn a star?", 42),
        settings=Settings(), client=client, base_url=BASE, secret=SIGNING,
    )
    by_topic = {b["topic"]: b for b in bodies}
    dana_actions = by_topic["dana-topic"]["actions"]
    assert [a["label"] for a in dana_actions] == ["⭐ Yes", "Not today"]
    tok = dana_actions[0]["url"].split("t=", 1)[1]
    assert verify_action(tok, SIGNING) == ("dana", "star_award", 42)  # signed for the recipient


@pytest.fixture()
def signed_client(store):
    app = create_app(store=store, settings=Settings(session_secret=SIGNING, oauth_base_url=BASE))
    with TestClient(app) as c:
        yield c


def test_nudge_act_star_award_adds_a_star_and_skip_is_a_noop(signed_client, store):
    dana = store.scoped(store.get_user("dana")["id"])
    aid = _star_agreement(dana, store)
    r = signed_client.get(f"/nudge/act?t={sign_action('dana', 'star_award', aid, SIGNING)}")
    assert r.status_code == 200 and "Star added" in r.text
    assert dana.star_total(aid) == 1
    # "Not today" changes nothing
    r2 = signed_client.get(f"/nudge/act?t={sign_action('alex', 'star_skip', aid, SIGNING)}")
    assert "No star today" in r2.text
    assert dana.star_total(aid) == 1


# --- weekly mental-load check-in: pure logic ---------------------------------


def test_normalize_checkin_config_validates():
    clean, err = normalize_checkin_config({"enabled": True, "day": 6, "time": "19:00"})
    assert err is None and clean == {"enabled": True, "day": 6, "time": "19:00"}
    # enabling without a day/time is rejected (would never fire)
    assert normalize_checkin_config({"enabled": True, "day": None, "time": None})[1]
    assert normalize_checkin_config({"enabled": True, "day": 9, "time": "19:00"})[1]
    # disabled may omit day/time (turning it off)
    off, e = normalize_checkin_config({"enabled": False})
    assert e is None and off["enabled"] is False


def test_checkin_due_weekday_time_and_weekly_dedup():
    cfg = {"enabled": True, "day": 6, "time": "19:00"}  # Sundays 7pm
    sun_8pm = datetime.datetime(2026, 7, 5, 20, 0)  # 2026-07-05 is a Sunday
    assert checkin_due(cfg, now_local=sun_8pm) is True
    assert checkin_due(cfg, now_local=datetime.datetime(2026, 7, 5, 18, 0)) is False  # early
    assert checkin_due(cfg, now_local=datetime.datetime(2026, 7, 6, 20, 0)) is False  # not Sun
    # already sent this ISO week → held
    assert checkin_due(
        cfg, now_local=sun_8pm, last_sent_local=datetime.datetime(2026, 7, 5, 19, 1)
    ) is False
    # a send last week does NOT hold this week
    assert checkin_due(
        cfg, now_local=sun_8pm, last_sent_local=datetime.datetime(2026, 6, 28, 19, 1)
    ) is True
    assert checkin_due({"enabled": False, "day": 6, "time": "19:00"}, now_local=sun_8pm) is False


def test_week_key_is_stable_per_iso_week():
    assert week_key(datetime.datetime(2026, 7, 5, 20, 0)) == week_key(
        datetime.datetime(2026, 7, 5, 8, 0)
    )
    assert week_key(datetime.datetime(2026, 7, 5)) != week_key(datetime.datetime(2026, 7, 13))


def test_checkin_summary_is_gentle_and_never_names():
    balanced = checkin_summary(["light", "balanced"])
    assert "balanced" in balanced.lower()
    heavier = checkin_summary(["heavy", "balanced"])
    assert "heavier for one of you" in heavier and "scorekeeping" in heavier
    both = checkin_summary(["heavy", "heavy"])
    assert "both of you" in both
    # never names a person
    for text in (balanced, heavier, both):
        assert "Dana" not in text and "Alex" not in text
    # a single reply still reads warmly
    assert checkin_summary(["heavy"]).startswith("Thanks for checking in")


# --- weekly mental-load check-in: repo + endpoints ---------------------------


def _due_checkin_body():
    """A config whose day/time is due right now in the server's timezone."""
    now_local = local_datetime(utcnow(), Settings().timezone)
    return {"enabled": True, "day": now_local.weekday(), "time": "00:00"}


def test_checkin_config_round_trips_and_records_responses(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    dana.set_checkin_config(enabled=True, day=6, time="19:00")
    cfg = alex.get_checkin_config()  # shared across co-parents
    assert cfg["enabled"] is True and cfg["day"] == 6 and cfg["time"] == "19:00"
    dana.record_checkin_response(week="2026-W27", user_id=dana_id, response="heavy")
    alex.record_checkin_response(week="2026-W27", user_id=alex_id, response="light")
    got = {r["by_name"]: r["response"] for r in dana.checkin_responses("2026-W27")}
    assert got == {"Dana": "heavy", "Alex": "light"}
    # re-tapping overwrites in place (no second row)
    dana.record_checkin_response(week="2026-W27", user_id=dana_id, response="balanced")
    assert len(dana.checkin_responses("2026-W27")) == 2


def test_set_checkin_endpoint_validates(client):
    ok = client.post(
        "/household/checkin",
        json={"enabled": True, "day": 6, "time": "19:00"},
        headers=_h("dana-tok"),
    )
    assert ok.status_code == 200 and ok.json()["checkin"]["day"] == 6
    assert client.post(
        "/household/checkin",
        json={"enabled": True, "day": None, "time": None},
        headers=_h("dana-tok"),
    ).status_code == 422
    # config is visible on the shared sheet payload
    sheet = client.get("/household/sheet", headers=_h("alex-tok")).json()
    assert sheet["checkin"]["enabled"] is True


def test_checkin_check_sends_once_per_week_to_both(client):
    check = "/webhooks/household/checkin/check"
    client.post("/household/checkin", json=_due_checkin_body(), headers=_h("dana-tok"))
    first = client.post(check, json={}, headers=_h("alex-tok")).json()
    assert first["sent"] is True
    assert {n["handle"] for n in first["notified"]} == {"dana", "alex"}
    # a second sweep the same week is a no-op
    second = client.post(check, json={}, headers=_h("dana-tok")).json()
    assert second["sent"] is False


def test_checkin_check_skips_when_off(client):
    assert client.post(
        "/webhooks/household/checkin/check", json={}, headers=_h("dana-tok")
    ).json()["sent"] is False


# --- weekly mental-load check-in: one-tap buttons + tap ----------------------


def test_checkin_notice_carries_three_signed_self_report_buttons(store):
    store.scoped(store.get_user("dana")["id"]).set_state("ntfy_topic", "dana-topic")
    store.scoped(store.get_user("alex")["id"]).set_state("ntfy_topic", "alex-topic")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={"id": "x"})

    client = DeliveryClient.from_settings(Settings(), transport=httpx.MockTransport(handler))
    hid = store.get_user("dana")["household_id"]
    deliver_to_household(
        store, hid, household_checkin_notice("How did it feel?"),
        settings=Settings(), client=client, base_url=BASE, secret=SIGNING,
    )
    actions = {b["topic"]: b["actions"] for b in bodies}["dana-topic"]
    assert [a["label"] for a in actions] == ["Felt light 🙂", "Balanced ⚖️", "Carried a lot 🫠"]
    tok = actions[2]["url"].split("t=", 1)[1]
    assert verify_action(tok, SIGNING) == ("dana", "load_heavy", 0)


def test_nudge_act_checkin_records_both_then_notes(signed_client, store):
    week = week_key(local_datetime(utcnow(), Settings().timezone))
    dana = store.scoped(store.get_user("dana")["id"])
    # first parent replies — recorded, no shared note yet
    r1 = signed_client.get(f"/nudge/act?t={sign_action('dana', 'load_balanced', 0, SIGNING)}")
    assert "Thanks for checking in" in r1.text
    assert len(dana.checkin_responses(week)) == 1
    # second parent replies — both now on record (the gentle note fans out)
    signed_client.get(f"/nudge/act?t={sign_action('alex', 'load_heavy', 0, SIGNING)}")
    got = {r["by_name"]: r["response"] for r in dana.checkin_responses(week)}
    assert got == {"Dana": "balanced", "Alex": "heavy"}


# --- daily delta digest: pure logic ------------------------------------------


def test_unseen_changes_excludes_own_and_filters_since(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    dana.set_fact(category="sizes", item="shoe size", value="13", updated_by=dana_id)
    alex.set_fact(category="food", item="allergy", value="peanuts", updated_by=alex_id)
    # From Alex's view: only Dana's change counts — never Alex's own edits.
    whats = [c["what"] for c in unseen_changes(alex, viewer_id=alex_id, since="")]
    assert any("shoe size" in w for w in whats)
    assert all("allergy" not in w for w in whats)
    # a future `since` filters everything out
    assert unseen_changes(alex, viewer_id=alex_id, since="2999-01-01 00:00:00") == []


def test_unseen_changes_spans_facts_agreements_and_stars(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    dana.set_fact(category="sizes", item="shoe size", value="13", updated_by=dana_id)
    aid = dana.set_agreement(title="Star chart", body="s", kind="reward", updated_by=dana_id)
    dana.award_stars(agreement_id=aid, delta=2, awarded_by=dana_id)
    kinds = [c["what"] for c in unseen_changes(alex, viewer_id=alex_id, since="")]
    assert any("shoe size" in w for w in kinds)
    assert any("(plan)" in w for w in kinds)
    assert any("⭐" in w for w in kinds)


def test_digest_message_lists_and_truncates():
    changes = [{"what": f"item {i}", "who": "Dana", "at": f"2026-07-{i:02d}"} for i in range(1, 9)]
    msg = digest_message(changes, max_items=3)
    assert "since you last looked" in msg
    assert msg.count("•") == 3
    assert "and 5 more" in msg


def test_digest_interval_ok_is_the_once_a_day_cap():
    now = datetime.datetime(2026, 7, 3, 12, 0)
    assert digest_interval_ok(None, now) is True
    assert digest_interval_ok("2026-07-03 09:00:00", now) is False  # 3h ago < 20h
    assert digest_interval_ok("2026-07-01 09:00:00", now) is True  # >20h ago


# --- daily delta digest: repo + endpoints ------------------------------------


def test_digest_enabled_toggle_and_star_awarded_by(store, dana):
    dana_id = store.get_user("dana")["id"]
    assert dana.get_digest_enabled() is False
    dana.set_digest_enabled(True)
    assert dana.get_digest_enabled() is True
    aid = _star_agreement(dana, store)
    dana.award_stars(agreement_id=aid, delta=1, awarded_by=dana_id)
    assert dana.recent_star_awards()[0]["awarded_by"] == dana_id  # id exposed for the diff


def test_sheet_reports_unseen_then_marks_it_seen(client):
    client.post(
        "/household/facts",
        json={"category": "sizes", "item": "shoe size", "value": "13"},
        headers=_h("dana-tok"),
    )
    # Alex's first look: 1 unseen (Dana's change); the look itself marks it seen.
    first = client.get("/household/sheet", headers=_h("alex-tok")).json()["digest"]
    assert first["unseen"] == 1
    second = client.get("/household/sheet", headers=_h("alex-tok")).json()["digest"]
    assert second["unseen"] == 0


def test_digest_check_sends_others_changes_and_self_suppresses(client):
    check = "/webhooks/household/digest/check"
    client.post("/household/digest", json={"enabled": True}, headers=_h("dana-tok"))
    client.post(
        "/household/facts",
        json={"category": "sizes", "item": "shoe size", "value": "13"},
        headers=_h("dana-tok"),
    )
    # Only Alex is nudged — it's Dana's own change, so Dana gets nothing.
    first = client.post(check, json={}, headers=_h("dana-tok")).json()
    assert [s["handle"] for s in first["sent"]] == ["alex"]
    assert first["sent"][0]["count"] == 1
    # A second sweep the same day is suppressed (once/day + already digested).
    second = client.post(check, json={}, headers=_h("alex-tok")).json()
    assert second["sent"] == []


def test_digest_check_silent_when_off(client):
    client.post(
        "/household/facts",
        json={"category": "sizes", "item": "shoe size", "value": "13"},
        headers=_h("dana-tok"),
    )
    resp = client.post("/webhooks/household/digest/check", json={}, headers=_h("alex-tok"))
    assert resp.json()["sent"] == []  # off by default


# --- daily delta digest: one-tap button + tap --------------------------------


def test_digest_notice_carries_signed_caught_up_button(store):
    dana = store.scoped(store.get_user("dana")["id"])
    dana.set_state("ntfy_topic", "dana-topic")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={"id": "x"})

    client = DeliveryClient.from_settings(Settings(), transport=httpx.MockTransport(handler))
    deliver_to_member(
        dana, household_digest_notice("here's what changed"),
        handle="dana", settings=Settings(), client=client, base_url=BASE, secret=SIGNING,
    )
    actions = bodies[0]["actions"]
    assert [a["label"] for a in actions] == ["Caught up 👍"]
    assert verify_action(actions[0]["url"].split("t=", 1)[1], SIGNING) == ("dana", "digest_seen", 0)


def test_nudge_act_digest_seen_marks_the_sheet_seen(signed_client, store):
    dana = store.scoped(store.get_user("dana")["id"])
    assert not dana.get_state("household_seen_at")
    r = signed_client.get(f"/nudge/act?t={sign_action('dana', 'digest_seen', 0, SIGNING)}")
    assert r.status_code == 200 and "caught up" in r.text.lower()
    assert dana.get_state("household_seen_at")


# --- load balance view -------------------------------------------------------


def test_balance_view_shares_and_gentle_captions():
    counts = [
        {"user_id": 1, "name": "Dana", "count": 9},
        {"user_id": 2, "name": "Alex", "count": 1},
    ]
    v = balance_view(counts)
    assert v["total"] == 10
    assert {m["name"]: m["share"] for m in v["members"]} == {"Dana": 90, "Alex": 10}
    # lopsided names only the *carrier*, never the low contributor
    assert "Dana" in v["caption"] and "carrying" in v["caption"]
    assert "Alex" not in v["caption"]
    even = balance_view(
        [{"user_id": 1, "name": "Dana", "count": 5}, {"user_id": 2, "name": "Alex", "count": 5}]
    )
    assert "evenly" in even["caption"].lower()
    empty = balance_view(
        [{"user_id": 1, "name": "Dana", "count": 0}, {"user_id": 2, "name": "Alex", "count": 0}]
    )
    assert empty["total"] == 0 and "nothing to compare" in empty["caption"].lower()


def test_contribution_counts_tallies_all_sources_and_keeps_zeros(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_fact(category="sizes", item="shoe size", value="13", updated_by=dana_id)
    aid = dana.set_agreement(title="Star chart", body="s", kind="reward", updated_by=dana_id)
    dana.award_stars(agreement_id=aid, delta=1, awarded_by=dana_id)
    # Alex did nothing, but still appears with a zero so the split shows both.
    counts = {c["name"]: c["count"] for c in dana.contribution_counts("")}
    assert counts == {"Dana": 3, "Alex": 0}
    # a future window excludes everything
    future = {c["name"]: c["count"] for c in dana.contribution_counts("2999-01-01 00:00:00")}
    assert future == {"Dana": 0, "Alex": 0}


def test_balance_enabled_toggle(dana):
    assert dana.get_balance_enabled() is False
    dana.set_balance_enabled(True)
    assert dana.get_balance_enabled() is True


def test_balance_endpoint_shared_only_with_toggle(client, store):
    body = client.get("/household/sheet", headers=_h("dana-tok")).json()["balance"]
    assert body["enabled"] is False and body["view"] is None  # off by default
    client.post(
        "/household/facts",
        json={"category": "sizes", "item": "shoe size", "value": "13"},
        headers=_h("dana-tok"),
    )
    assert client.post(
        "/household/balance", json={"enabled": True}, headers=_h("alex-tok")
    ).json()["enabled"] is True
    view = client.get("/household/sheet", headers=_h("alex-tok")).json()["balance"]["view"]
    assert view["total"] == 1
    assert {m["name"] for m in view["members"]} == {"Dana", "Alex"}


def test_balance_absent_for_single_parent(client, store):
    solo = store.create_household("Solo")
    store.set_user_household("lee", solo)
    assert client.get("/household/sheet", headers=_h("lee-tok")).json()["balance"] is None


# --- single-parent households ------------------------------------------------


def test_member_count_and_is_shared(store, dana):
    assert dana.household_member_count() == 2  # dana + alex
    assert dana.is_shared_household() is True
    solo = store.create_household("Solo")
    store.set_user_household("lee", solo)
    lee = store.scoped(store.get_user("lee")["id"])
    assert lee.household_member_count() == 1
    assert lee.is_shared_household() is False


def test_single_parent_disables_load_balancing(client, store):
    """A household of one skips the co-parent-only features, even if toggled on."""
    solo = store.create_household("Solo")
    store.set_user_household("lee", solo)
    assert client.get("/household/sheet", headers=_h("lee-tok")).json()["shared"] is False
    # Turn both on and make them "due" — the sweeps must still stay silent.
    client.post("/household/checkin", json=_due_checkin_body(), headers=_h("lee-tok"))
    client.post("/household/digest", json={"enabled": True}, headers=_h("lee-tok"))
    client.post(
        "/household/facts",
        json={"category": "sizes", "item": "shoe size", "value": "9"},
        headers=_h("lee-tok"),
    )
    assert client.post(
        "/webhooks/household/checkin/check", json={}, headers=_h("lee-tok")
    ).json()["sent"] is False
    assert client.post(
        "/webhooks/household/digest/check", json={}, headers=_h("lee-tok")
    ).json()["sent"] == []


def test_single_parent_still_gets_star_features(client, store):
    """Star tracking + congratulation aren't load-balancing — they stay on solo."""
    solo = store.create_household("Solo")
    store.set_user_household("lee", solo)
    aid = client.post(
        "/household/agreements",
        json={"title": "Star chart", "kind": "reward", "structured": STAR_CHART},
        headers=_h("lee-tok"),
    ).json()["id"]
    hit = client.post(
        f"/household/agreements/{aid}/stars", json={"delta": 5}, headers=_h("lee-tok")
    ).json()
    assert hit["total"] == 5
    assert [g["reward"] for g in hit["goals_reached"]] == ["movie night"]
    assert {n["handle"] for n in hit["notified"]} == {"lee"}  # the one parent is still told


def test_load_balancing_activates_when_a_second_parent_joins(client, store):
    solo = store.create_household("Solo")
    store.set_user_household("lee", solo)
    assert client.get("/household/sheet", headers=_h("lee-tok")).json()["shared"] is False
    store.set_user_household("op", solo)  # a co-parent joins
    assert client.get("/household/sheet", headers=_h("lee-tok")).json()["shared"] is True


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


def test_last_prompted_at_backfilled_on_old_db(tmp_path):
    """An agreements table predating the prompt schedule gains last_prompted_at."""
    db = str(tmp_path / "old.db")
    conn = connect(db)
    conn.execute(
        "CREATE TABLE household_agreements (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "household_id INTEGER, child_id INTEGER, title TEXT, kind TEXT, body TEXT, "
        "structured TEXT, updated_by INTEGER, updated_at DATETIME)"
    )
    conn.commit()
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(household_agreements)")}
    assert "last_prompted_at" in cols
    conn.close()


def test_checkin_columns_backfilled_on_old_db(tmp_path):
    """A households table predating the check-in gains its config columns."""
    db = str(tmp_path / "old.db")
    conn = connect(db)
    conn.execute(
        "CREATE TABLE households (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
        "created_at DATETIME)"
    )
    conn.commit()
    backfill_added_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(households)")}
    assert {
        "checkin_enabled", "checkin_day", "checkin_time", "checkin_last_sent_at",
        "digest_enabled", "balance_enabled",
    } <= cols
    conn.close()
