"""Tests for the freeform "find me a time" calendar assistant.

Covers the three layers of :mod:`prefrontal.availability`: interpreting free text
into a request (heuristic + model + the clarifying-question path), scoping the
blocking constraints to who's involved (the FYI-only-when-a-partner-is-there
rule), and the end-to-end plan against a store — plus the HTTP endpoint.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from prefrontal.availability import (
    AVAILABILITY_SYSTEM,
    ScheduleRequest,
    constraint_commitments,
    find_availability,
    interpret_request,
    plan_availability,
    render_plan,
)
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore, init_db, provision_user
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

SECRET = "test-secret"


class _FakeClient:
    """A model stand-in: available, returns a fixed reply (or raises)."""

    def __init__(self, reply: str = "", *, error: bool = False) -> None:
        self._reply = reply
        self._error = error

    def available(self) -> bool:
        return True

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if self._error:
            raise OllamaError("down")
        return self._reply


def _commit(start_at, end_at=None, *, kind="self", title="Event"):
    """Minimal commitment dict for the constraint/slot helpers."""
    return {"start_at": start_at, "end_at": end_at, "kind": kind, "title": title}


# --- constraint_commitments: who's involved → what blocks -------------------


def test_partner_fyi_blocks_only_when_partner_is_involved():
    commitments = [
        _commit("2026-07-12 10:00:00", "2026-07-12 11:00:00", kind="self"),
        _commit("2026-07-12 14:00:00", "2026-07-12 15:00:00", kind="fyi"),
    ]
    solo, ignored = constraint_commitments(commitments, with_partner=False)
    assert [c["kind"] for c in solo] == ["self"]  # the FYI is dropped
    assert ignored == 1

    both, ignored_both = constraint_commitments(commitments, with_partner=True)
    assert sorted(c["kind"] for c in both) == ["fyi", "self"]  # both block
    assert ignored_both == 0


def test_child_commitment_always_blocks():
    # A kid's appointment is a real obligation someone must cover — it blocks the
    # user whether or not a partner is part of the plan.
    commitments = [_commit("2026-07-12 09:00:00", "2026-07-12 10:00:00", kind="child")]
    solo, ignored = constraint_commitments(commitments, with_partner=False)
    assert len(solo) == 1 and ignored == 0


def test_placeholder_holds_never_block():
    # "HOLD"/"Focus" blocks are elastic time you'd yield — excluded like everywhere.
    commitments = [_commit("2026-07-12 09:00:00", "2026-07-12 10:00:00", title="HOLD")]
    solo, _ = constraint_commitments(commitments, with_partner=False)
    assert solo == []


# --- interpret_request: heuristic parse + clarifying questions --------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("find 45 min today", 45.0),
        ("block 30 minutes", 30.0),
        ("an hour for a call", 60.0),
        ("2 hours to focus", 120.0),
        ("1.5 hours this week", 90.0),
        ("half an hour tomorrow", 30.0),
        ("90m for the gym", 90.0),
        ("an hour and a half for a walk", 90.0),
        ("2 hours and a half this week", 150.0),
        ("2 and a half hours to focus", 150.0),
    ],
)
def test_heuristic_parses_duration(message, expected):
    request, question = interpret_request(message)
    assert question is None
    assert request is not None and request.minutes == expected


def test_missing_duration_asks_a_question():
    request, question = interpret_request("find time for coffee this week")
    assert request is None
    assert question and "how long" in question.lower()


def test_heuristic_detects_partner_from_words():
    for message in (
        "45 min for dinner with my wife",
        "when are we both free for 30 min",
        "an hour for date night",
    ):
        request, _ = interpret_request(message)
        assert request is not None and request.with_partner is True


def test_heuristic_solo_by_default():
    request, _ = interpret_request("find 30 min for a run")
    assert request is not None and request.with_partner is False


def test_partner_name_flips_involvement():
    # A co-parent's first name in the ask counts as "involves the partner".
    request, _ = interpret_request(
        "grab 45 min with dana", partner_names=["dana"]
    )
    assert request is not None and request.with_partner is True


def test_heuristic_maps_timeframe_and_band():
    request, _ = interpret_request("30 min tomorrow afternoon")
    assert request is not None
    assert request.days == 2
    assert request.time_window == ("12:00", "17:00")


def test_duration_is_clamped_not_rejected():
    request, _ = interpret_request("block 5000 minutes this week")
    assert request is not None and request.minutes == 24 * 60.0


# --- interpret_request: model path ------------------------------------------


def test_model_path_parses_json():
    client = _FakeClient(
        json.dumps(
            {
                "minutes": 60,
                "days": 7,
                "with_partner": True,
                "time_of_day": {"start": "17:00", "end": "22:00"},
                "title": "Dinner out",
                "question": None,
            }
        )
    )
    request, question = interpret_request("dinner sometime this week", client=client)
    assert question is None
    assert request == ScheduleRequest(
        minutes=60.0,
        days=7,
        with_partner=True,
        title="Dinner out",
        time_window=("17:00", "22:00"),
    )


def test_model_question_is_surfaced():
    client = _FakeClient(
        json.dumps({"minutes": None, "question": "How long do you need?"})
    )
    request, question = interpret_request("find time to chat", client=client)
    assert request is None
    assert question == "How long do you need?"


def test_model_down_falls_back_to_heuristic():
    # The model raises; the offline heuristic still pulls "45 min" out of the ask.
    request, question = interpret_request(
        "45 min for a call today", client=_FakeClient(error=True)
    )
    assert question is None
    assert request is not None and request.minutes == 45.0


def test_system_prompt_forbids_the_model_picking_a_time():
    assert "Do NOT pick a time" in AVAILABILITY_SYSTEM


# --- find_availability: slots respect the participant filter ----------------


def test_find_availability_solo_sees_through_partner_fyi():
    now = datetime(2026, 7, 12, 9, 0, 0)
    commitments = [
        _commit("2026-07-12 10:00:00", "2026-07-12 11:00:00", kind="self"),
        _commit("2026-07-12 14:00:00", "2026-07-12 15:00:00", kind="fyi"),
    ]
    request = ScheduleRequest(minutes=30, days=1)
    slots, considered, ignored = find_availability(
        commitments, request, now=now, tz="UTC"
    )
    assert considered == 1 and ignored == 1
    # The 14:00 FYI does not carve the afternoon: 11:00→22:00 is one open window.
    assert ("2026-07-12 11:00:00", "2026-07-12 22:00:00") in [
        (s.start, s.end) for s in slots
    ]


def test_find_availability_with_partner_carves_around_fyi():
    now = datetime(2026, 7, 12, 9, 0, 0)
    commitments = [
        _commit("2026-07-12 10:00:00", "2026-07-12 11:00:00", kind="self"),
        _commit("2026-07-12 14:00:00", "2026-07-12 15:00:00", kind="fyi"),
    ]
    request = ScheduleRequest(minutes=30, days=1, with_partner=True)
    slots, considered, ignored = find_availability(
        commitments, request, now=now, tz="UTC"
    )
    assert considered == 2 and ignored == 0
    spans = [(s.start, s.end) for s in slots]
    # Now the FYI blocks: the afternoon splits at 14:00–15:00.
    assert ("2026-07-12 11:00:00", "2026-07-12 14:00:00") in spans
    assert ("2026-07-12 15:00:00", "2026-07-12 22:00:00") in spans


def test_time_window_restricts_the_daily_band():
    now = datetime(2026, 7, 12, 6, 0, 0)
    request = ScheduleRequest(minutes=30, days=1, time_window=("13:00", "16:00"))
    slots, _, _ = find_availability([], request, now=now, tz="UTC")
    assert [(s.start, s.end) for s in slots] == [
        ("2026-07-12 13:00:00", "2026-07-12 16:00:00")
    ]


# --- plan_availability: end-to-end against a store --------------------------


@pytest.fixture()
def memory():
    conn = init_db(":memory:")
    store = MemoryStore(conn)
    scoped = scoped_default(store)
    try:
        yield scoped
    finally:
        conn.close()


def _seed(memory):
    memory.upsert_commitment(
        title="Standup", start_at="2026-07-12 10:00:00",
        end_at="2026-07-12 11:00:00", source="manual", kind="self",
    )
    memory.upsert_commitment(
        title="Partner appt", start_at="2026-07-12 14:00:00",
        end_at="2026-07-12 15:00:00", source="manual", kind="fyi",
    )


def test_plan_ignores_partner_fyi_when_solo(memory):
    _seed(memory)
    now = datetime(2026, 7, 12, 9, 0, 0)
    plan = plan_availability("find 30 min today", memory, now=now, tz="UTC")
    assert plan.question is None
    assert plan.with_partner is False
    assert plan.ignored_fyi == 1
    # An afternoon slot spanning the partner's appt is offered (it doesn't block us).
    assert any(s.start <= "2026-07-12 14:00:00" <= s.end for s in plan.slots)


def test_plan_respects_partner_fyi_when_both(memory):
    _seed(memory)
    now = datetime(2026, 7, 12, 9, 0, 0)
    plan = plan_availability(
        "find 30 min for the two of us today", memory, now=now, tz="UTC"
    )
    assert plan.with_partner is True and plan.ignored_fyi == 0
    # No offered slot straddles the partner's now-blocking appointment.
    assert not any(s.start < "2026-07-12 15:00:00" and s.end > "2026-07-12 14:00:00"
                   for s in plan.slots)


def test_plan_asks_when_underspecified(memory):
    _seed(memory)
    plan = plan_availability("find time to catch up", memory, tz="UTC")
    assert plan.question is not None and not plan.slots


def test_plan_echoes_participants_even_while_asking(memory):
    # No duration → we ask, but still report that both of us are involved, so the
    # client needn't re-ask "is this for both of you?".
    _seed(memory)
    plan = plan_availability("when are we both free to catch up", memory, tz="UTC")
    assert plan.question is not None
    assert plan.with_partner is True


# --- render_plan ------------------------------------------------------------


def test_render_question_is_just_the_question():
    from prefrontal.availability import AvailabilityPlan

    plan = AvailabilityPlan(question="How long should I set aside?")
    assert render_plan(plan) == "How long should I set aside?"


def test_render_slots_lists_them_with_the_ignored_note():
    now = datetime(2026, 7, 12, 9, 0, 0)
    commitments = [_commit("2026-07-12 14:00:00", "2026-07-12 15:00:00", kind="fyi")]
    request = ScheduleRequest(minutes=30, days=1)
    slots, considered, ignored = find_availability(
        commitments, request, now=now, tz="UTC"
    )
    from prefrontal.availability import AvailabilityPlan

    text = render_plan(
        AvailabilityPlan(request=request, slots=slots, considered=considered,
                         ignored_fyi=ignored),
        "UTC",
    )
    assert "Open 30-min slots" in text
    assert "Ignored 1 FYI item that is someone else's" in text


def test_render_no_slots_suggests_widening():
    request = ScheduleRequest(minutes=30, days=1)
    from prefrontal.availability import AvailabilityPlan

    text = render_plan(AvailabilityPlan(request=request, slots=[]), "UTC")
    assert "No 30-min slot fits" in text


# --- endpoint ---------------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="Tester", token=SECRET, is_operator=True)
    try:
        yield unscoped
    finally:
        conn.close()


@pytest.fixture()
def user_store(store):
    return store.scoped(store.get_user("tester")["id"])


def _client(store, fake=None):
    app = create_app(store=store, settings=Settings(), anthropic=fake)
    return TestClient(app)


def test_find_time_requires_auth(store):
    with _client(store) as c:
        assert c.post("/assistant/find-time", json={"message": "hi"}).status_code == 401


def test_find_time_endpoint_returns_slots(store, user_store):
    user_store.upsert_commitment(
        title="Standup", start_at="2026-07-12 10:00:00",
        end_at="2026-07-12 11:00:00", source="manual", kind="self",
    )
    # No model client → the offline heuristic parses "45 min".
    with _client(store) as c:
        resp = c.post(
            "/assistant/find-time",
            json={"message": "find 45 min this week"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"] is None
    assert body["request"]["minutes"] == 45.0
    assert body["request"]["with_partner"] is False
    assert isinstance(body["slots"], list) and body["slots"]
    assert "start" in body["slots"][0] and "day" in body["slots"][0]


def test_find_time_endpoint_asks_a_question(store, user_store):
    with _client(store) as c:
        resp = c.post(
            "/assistant/find-time",
            json={"message": "find time to chat"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["question"] and body["slots"] == []
    assert body["request"] is None
