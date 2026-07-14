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


def _today_at(hour: int) -> str:
    """A fixed UTC time today — always inside the local day the sick-day tool bounds
    to, whatever wall-clock hour the suite runs at (unlike a `now`-relative offset,
    which can spill past UTC midnight)."""
    return utcnow().replace(hour=hour, minute=0, second=0, microsecond=0).strftime(TS_FMT)


# -- registry ----------------------------------------------------------------


def test_parent_pack_declares_its_situation_tools():
    from prefrontal.packs import get as get_pack

    parent = get_pack("parent")
    assert [t.key for t in parent.situations] == ["school_run", "pack_the_bag", "sick_day"]


def test_situations_visible_only_when_owning_pack_enabled():
    # No pack: nothing to run.
    assert enabled_situations(Settings(packs=())) == []
    assert get_situation("school_run", Settings(packs=())) is None
    # Parent on: all its tools are exposed.
    on = Settings(packs=("parent",))
    assert [t.key for t in enabled_situations(on)] == ["school_run", "pack_the_bag", "sick_day"]
    assert get_situation("school_run", on) is not None
    assert get_situation("pack_the_bag", on) is not None
    assert get_situation("sick_day", on) is not None
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


# -- the pack-the-bag handler ------------------------------------------------


def test_pack_the_bag_builds_a_checklist_per_upcoming_child_event(store):
    # Two kid events soon (child), a self commitment (excluded), and an fyi
    # (never attendable, excluded). No model client → deterministic first steps.
    a_id, _ = store.upsert_commitment(title="Swimming lesson", start_at=_in(90))
    store.set_commitment_kind(a_id, "child", "user")
    b_id, _ = store.upsert_commitment(title="Football practice", start_at=_in(180))
    store.set_commitment_kind(b_id, "child", "user")
    store.upsert_commitment(title="My 1:1", start_at=_in(90))  # self, excluded
    fyi_id, _ = store.upsert_commitment(title="Partner's yoga", start_at=_in(90))
    store.set_commitment_kind(fyi_id, "fyi", "user")

    tool = get_situation("pack_the_bag", Settings(packs=("parent",)))
    result = tool.handler(store)

    assert result["tool"] == "pack_the_bag"
    titles = [c["title"] for c in result["checklists"]]
    assert titles == ["Swimming lesson", "Football practice"]  # soonest first, kids only
    first = result["checklists"][0]
    assert first["commitment_id"] == a_id
    assert first["first_step"]  # a concrete first step was produced
    assert first["source"] == "heuristic"  # no client → deterministic fallback
    # The nearest event's first step bubbles up as the push headline.
    assert result["headline"] == (
        f"Getting Swimming lesson ready — start here: {first['first_step']}"
    )


def test_pack_the_bag_ignores_events_past_the_horizon(store):
    from prefrontal.packs.parent import PACK_THE_BAG_HORIZON_HOURS

    soon_id, _ = store.upsert_commitment(title="Dentist", start_at=_in(120))
    store.set_commitment_kind(soon_id, "child", "user")
    far_id, _ = store.upsert_commitment(
        title="Sports day", start_at=_in(PACK_THE_BAG_HORIZON_HOURS * 60 + 120)
    )
    store.set_commitment_kind(far_id, "child", "user")

    tool = get_situation("pack_the_bag", Settings(packs=("parent",)))
    result = tool.handler(store)
    assert [c["title"] for c in result["checklists"]] == ["Dentist"]


def test_pack_the_bag_empty_when_no_kid_events(store):
    tool = get_situation("pack_the_bag", Settings(packs=("parent",)))
    result = tool.handler(store)
    assert result["checklists"] == []
    assert result["headline"] == ""


# -- the sick-day handler ----------------------------------------------------


def test_sick_day_splits_today_by_hardness_and_gives_a_first_step(store):
    # A hard obligation you still have to cover, and a soft block you can drop.
    store.upsert_commitment(title="Client call", start_at=_today_at(12), hardness="hard")
    store.upsert_commitment(title="Gym", start_at=_today_at(18), hardness="soft")
    # An overdue todo gives the panic triage a first step to surface.
    store.add_todo("File the expense report", deadline="2000-01-01")

    tool = get_situation("sick_day", Settings(packs=("parent",)))
    result = tool.handler(store)

    assert result["tool"] == "sick_day"
    assert [c["title"] for c in result["must_cover"]] == ["Client call"]
    assert [c["title"] for c in result["can_reschedule"]] == ["Gym"]
    assert result["first_step"]  # the overdue todo yields a concrete first step
    assert result["headline"].startswith("Kid's home sick — 1 thing still needs covering today.")


def test_sick_day_calm_when_nothing_is_locked_in(store):
    # Only a soft block today, nothing pressing: the headline reassures.
    store.upsert_commitment(title="Optional coffee", start_at=_today_at(14), hardness="soft")

    tool = get_situation("sick_day", Settings(packs=("parent",)))
    result = tool.handler(store)
    assert result["must_cover"] == []
    assert [c["title"] for c in result["can_reschedule"]] == ["Optional coffee"]
    assert "clear the day" in result["headline"]


# -- the /packs/situations router --------------------------------------------


def test_list_situations_reflects_enabled_pack(store):
    with _client(store) as c:  # no pack
        assert c.get("/packs/situations", headers=_auth()).json() == {"situations": []}
    with _client(store, packs=("parent",)) as c:
        body = c.get("/packs/situations", headers=_auth()).json()
    assert [s["tool"] for s in body["situations"]] == ["school_run", "pack_the_bag", "sick_day"]
    assert body["situations"][0]["title"] == "School run"


def test_run_decomposing_tool_falls_back_when_the_model_is_unreachable(store):
    # The router hands the tool the local Ollama client. With no Ollama running
    # (as in CI), decompose_task's generate() raises OllamaError and the tool must
    # fall back to the heuristic — the endpoint returns 200, never a 500. Guards
    # against re-introducing an Anthropic client here, whose AnthropicError
    # (sibling of OllamaError) decompose_task does not catch.
    kid_id, _ = store.upsert_commitment(title="Swim meet", start_at=_in(90))
    store.set_commitment_kind(kid_id, "child", "user")
    with _client(store, packs=("parent",)) as c:
        r = c.post("/packs/situations/pack_the_bag", headers=_auth())
    assert r.status_code == 200
    checklists = r.json()["checklists"]
    assert [c["title"] for c in checklists] == ["Swim meet"]
    assert checklists[0]["source"] == "heuristic"
    assert checklists[0]["first_step"]


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


# -- the Caregiver pack ------------------------------------------------------


def test_caregiver_pack_declares_its_situation_tools():
    from prefrontal.packs import get as get_pack

    care = get_pack("caregiver")
    assert [t.key for t in care.situations] == ["care_run", "paperwork", "respite"]


def test_caregiver_situations_visible_only_when_enabled():
    on = Settings(packs=("caregiver",))
    assert [t.key for t in enabled_situations(on)] == ["care_run", "paperwork", "respite"]
    # Off with no pack; and the Parent pack doesn't expose the Caregiver tools.
    assert get_situation("care_run", Settings(packs=())) is None
    assert get_situation("respite", Settings(packs=("parent",))) is None


def test_care_run_plans_care_departures_and_ignores_the_rest(store):
    # The care recipient's appointment (care) — should plan; a self commitment and
    # a kid's event (child) — must not (this is the caregiver's run, not theirs).
    care_id, _ = store.upsert_commitment(title="Mom's cardiology", start_at=_in(20))
    store.set_commitment_kind(care_id, "care", "user")
    store.upsert_commitment(title="My standup", start_at=_in(20))  # self, excluded
    kid_id, _ = store.upsert_commitment(title="School run", start_at=_in(20))
    store.set_commitment_kind(kid_id, "child", "user")

    tool = get_situation("care_run", Settings(packs=("caregiver",)))
    result = tool.handler(store)

    assert result["tool"] == "care_run"
    assert [d["title"] for d in result["departures"]] == ["Mom's cardiology"]
    only = result["departures"][0]
    assert only["commitment_id"] == care_id
    assert only["leave_by"]  # a leave-by was computed
    assert only["level"] != "none"  # soon enough to be reminder-worthy
    assert result["headline"] == only["message"]


def test_care_run_headline_empty_when_nothing_pressing(store):
    far_id, _ = store.upsert_commitment(title="Annual review", start_at="2099-01-01 09:00:00")
    store.set_commitment_kind(far_id, "care", "user")

    tool = get_situation("care_run", Settings(packs=("caregiver",)))
    result = tool.handler(store)
    assert [d["title"] for d in result["departures"]] == ["Annual review"]
    assert result["departures"][0]["level"] == "none"
    assert result["headline"] == ""


def test_paperwork_decomposes_open_admin_todos_only(store):
    # Two admin todos (the pile), a non-admin todo (excluded), and a delegated admin
    # todo (off your plate → excluded). No model client → deterministic first steps.
    store.add_todo("Appeal the insurance denial", category="admin", priority=2)
    store.add_todo("Submit the benefits renewal", category="admin", priority=1)
    store.add_todo("Buy groceries", category="caregiving")  # not admin, excluded
    handed_off = store.add_todo("Have the lawyer file the POA", category="admin")
    store.set_delegation(handed_off, handler="human", status="forwarded")

    tool = get_situation("paperwork", Settings(packs=("caregiver",)))
    result = tool.handler(store)

    assert result["tool"] == "paperwork"
    # Priority-ordered, admin-only, delegated one dropped.
    titles = [c["title"] for c in result["checklists"]]
    assert titles == ["Appeal the insurance denial", "Submit the benefits renewal"]
    first = result["checklists"][0]
    assert first["first_step"]
    assert first["source"] == "heuristic"  # no client → deterministic fallback
    assert result["headline"] == f"Admin pile — start here: {first['first_step']}"


def test_paperwork_caps_the_pile(store):
    from prefrontal.packs.caregiver import MAX_PAPERWORK_TODOS

    for i in range(MAX_PAPERWORK_TODOS + 2):
        store.add_todo(f"Admin task {i}", category="admin")

    tool = get_situation("paperwork", Settings(packs=("caregiver",)))
    result = tool.handler(store)
    assert len(result["checklists"]) == MAX_PAPERWORK_TODOS


def test_paperwork_empty_when_no_admin_pile(store):
    store.add_todo("Refill the prescription", category="medical")  # not admin
    tool = get_situation("paperwork", Settings(packs=("caregiver",)))
    result = tool.handler(store)
    assert result["checklists"] == []
    assert result["headline"] == ""


def test_respite_flags_skipped_basics_next_to_the_pressing_thing(store):
    # Arm self-care and force the meal check overdue regardless of the wall-clock
    # hour the suite runs at: start hour 0 (always past it) with a once-a-day target
    # and no confirms today. A pressing overdue todo gives the panic side a first step.
    store.set_state("self_care", "on")
    store.set_state("meal_start_hour", "0")
    store.set_state("meal_daily_target", "1")
    store.add_todo("Call the pharmacy about the refill", deadline="2000-01-01")

    tool = get_situation("respite", Settings(packs=("caregiver",)))
    result = tool.handler(store)

    assert result["tool"] == "respite"
    assert result["self_care_on"] is True
    assert "meal" in [s["key"] for s in result["skipped"]]
    assert result["pressing"] >= 1
    assert result["first_step"]
    assert result["first_step_for"] == "Call the pharmacy about the refill"
    # The headline pairs the skipped basics with the one thing that needs you.
    assert "behind on" in result["headline"]
    assert "meals" in result["headline"]
    assert "the one thing that truly needs you: Call the pharmacy" in result["headline"]
    assert "Take five for yourself." in result["headline"]


def test_respite_points_at_the_switch_when_self_care_is_off(store):
    # Master switch off (the module default): there's no "skipped" signal to give, so
    # respite surfaces the load counterweight and points at the switch. Deterministic
    # regardless of the hour.
    store.set_state("self_care", "off")
    store.add_todo("Chase the specialist referral", deadline="2000-01-01")

    tool = get_situation("respite", Settings(packs=("caregiver",)))
    result = tool.handler(store)

    assert result["self_care_on"] is False
    assert result["skipped"] == []
    assert result["headline"].startswith("Turn self-care checks on")
    assert "the one thing that truly needs you: Chase the specialist referral" in result["headline"]


def test_caregiver_situations_run_through_the_router(store):
    care_id, _ = store.upsert_commitment(title="Dad's dialysis", start_at=_in(20))
    store.set_commitment_kind(care_id, "care", "user")
    with _client(store, packs=("caregiver",)) as c:
        listed = c.get("/packs/situations", headers=_auth()).json()
        assert [s["tool"] for s in listed["situations"]] == ["care_run", "paperwork", "respite"]
        r = c.post("/packs/situations/care_run", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["tool"] == "care_run"
    assert [d["title"] for d in body["departures"]] == ["Dad's dialysis"]
