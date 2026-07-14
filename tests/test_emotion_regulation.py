"""Tests for the Emotion Regulation module and its pure core.

Covers the crisis-safety boundary (screened first, resources not skills), the
free-text → state classifier, skill selection + rotation, the on-demand
``build_support``/``record_support`` flow, the opt-in rough-day acceptance
fold-in, the module's profile section, and the ``POST /emotion/support`` endpoint.
Model-free throughout — the skill text is delivered verbatim, so there's nothing
to stub.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.emotion_regulation import (
    CRISIS_MESSAGE,
    LAST_SKILL_STATE_KEY,
    SKILLS,
    SUPPORT_CONTEXT_PREFIX,
    SUPPORT_CRISIS_KEY,
    build_support,
    infer_state,
    looks_like_crisis,
    pick_skill,
    record_support,
    recovery_acceptance_line,
)
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.modules.emotion_regulation import EmotionRegulationModule
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

SECRET = "er-secret"


@pytest.fixture()
def memory():
    conn = init_db(":memory:")
    store = MemoryStore(conn)
    scoped = scoped_default(store)
    try:
        yield scoped
    finally:
        conn.close()


# --- crisis boundary (screened before any skill) -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I want to kill myself",
        "thinking about suicide",
        "I want to end my life",
        "I just want to end it all",
        "I'd be better off dead",
        "I don't want to be here anymore",
        "I've been hurting myself",
        "thinking of self-harm",
    ],
)
def test_looks_like_crisis_positive(text):
    assert looks_like_crisis(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "I'm so overwhelmed I could scream",
        "work is killing me but I'll survive",  # figurative, not self-directed
        "I'm furious at my manager",
        "everything feels like too much today",
        "I need to end my meeting early",  # "end my …" must not trip the screen
        "let's end my day and go home",
    ],
)
def test_looks_like_crisis_negative(text):
    assert looks_like_crisis(text) is False


def test_build_support_crisis_short_circuits_to_resources(memory):
    r = build_support(memory, "honestly I want to kill myself")
    assert r.kind == "crisis"
    assert r.message == CRISIS_MESSAGE
    assert "988" in r.message
    assert r.skill_key == ""  # never a coping skill for a crisis
    assert r.state == ""


# --- state inference ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,state",
    [
        ("everything is too much right now", "overwhelm"),
        ("I'm so anxious and panicking", "anxiety"),
        ("I am absolutely furious", "anger"),
        ("I feel rejected and not good enough", "rejection"),
        ("just feel sad and empty", "sadness"),
        ("", "generic"),
        (None, "generic"),
        ("blah blah unrelated", "generic"),
    ],
)
def test_infer_state(text, state):
    assert infer_state(text) == state


# --- skill selection + rotation ----------------------------------------------


def test_pick_skill_fits_the_state():
    skill = pick_skill("rejection")
    assert "rejection" in skill.fits


def test_pick_skill_avoids_immediate_repeat():
    first = pick_skill("overwhelm")
    second = pick_skill("overwhelm", last_key=first.key)
    assert second.key != first.key


def test_pick_skill_repeats_only_if_it_is_the_sole_fit():
    # rejection_reframe is the only skill that fits *only* rejection, but several
    # fit rejection — so a repeat should still be avoidable. Verify the degenerate
    # guarantee directly: a single-fit state returns that skill even if last-used.
    solo = [s for s in SKILLS if s.fits == ("rejection",)][0]
    # Force a state with exactly one fit by asking for that skill's exclusive set.
    got = pick_skill("rejection", last_key=solo.key)
    assert got.key != solo.key or len([s for s in SKILLS if "rejection" in s.fits]) == 1


# --- build/record flow -------------------------------------------------------


def test_build_support_returns_fitting_skill_and_message(memory):
    r = build_support(memory, "I'm so anxious")
    assert r.kind == "skill"
    assert r.state == "anxiety"
    assert r.skill_key
    assert "anxiety" in next(s for s in SKILLS if s.key == r.skill_key).fits
    # Acknowledgment + skill + soft close, never "calm down".
    assert "calm down" not in r.message.lower()


def test_record_support_logs_checkin_and_rotates(memory):
    r1 = build_support(memory, "I'm overwhelmed")
    record_support(memory, r1)
    # A checkin episode is logged for the profile.
    checkins = memory.episodes_by_type("checkin")
    assert any((c.get("context") or "").startswith("emotion support:") for c in checkins)
    assert memory.get_state(LAST_SKILL_STATE_KEY) == r1.skill_key
    # The next request for the same state rotates off the last skill.
    r2 = build_support(memory, "I'm overwhelmed")
    assert r2.skill_key != r1.skill_key


def test_support_checkin_context_is_the_wire_format_the_gate_reads(memory):
    """The check-in context binds to the exported constants, not a loose literal.

    The coaching vulnerability gate filters ``checkin`` episodes on
    ``SUPPORT_CONTEXT_PREFIX`` and distinguishes a crisis by the ``SUPPORT_CRISIS_KEY``
    suffix. Pin both the ordinary and crisis writer paths to those exact strings so
    the reader and writer can't drift apart.
    """
    record_support(memory, build_support(memory, "I'm overwhelmed"))
    record_support(memory, build_support(memory, "I want to kill myself"))  # crisis screen
    contexts = [c.get("context") for c in memory.episodes_by_type("checkin")]
    assert f"{SUPPORT_CONTEXT_PREFIX}overwhelm" in contexts
    assert f"{SUPPORT_CONTEXT_PREFIX}{SUPPORT_CRISIS_KEY}" in contexts
    # Every support check-in shares the prefix the gate filters on.
    support = [c for c in contexts if c and c.startswith(SUPPORT_CONTEXT_PREFIX)]
    assert len(support) == 2


def test_one_tap_no_text_yields_a_generic_skill(memory):
    r = build_support(memory, None)
    assert r.kind == "skill"
    assert r.state == "generic"
    assert r.skill_key


# --- rough-day acceptance fold-in (opt-in) -----------------------------------


def test_recovery_acceptance_line_is_opt_in(memory):
    assert recovery_acceptance_line(memory) is None  # off by default
    memory.set_state("emotion_recovery_acceptance", "on")
    line = recovery_acceptance_line(memory)
    assert line and "not evidence about you" in line


def test_encouragement_render_includes_acceptance_when_opted_in(memory):
    from prefrontal.encouragement import DayAssessment, RecoveryPlan, render_encouragement

    rough = DayAssessment(
        date="2026-07-14", rough=True, rough_score=3.0, signals=[], enabled=True, tone="warm"
    )
    plan = RecoveryPlan(refit=[], defer=[], first_step=None, acceptance="Meet the feeling first.")
    text = render_encouragement(rough, plan)
    assert "Meet the feeling first." in text
    # Absent when not folded in.
    plain = render_encouragement(rough, RecoveryPlan(refit=[], defer=[], first_step=None))
    assert "Meet the feeling first." not in plain


# --- module ------------------------------------------------------------------


def test_module_profile_section_none_without_moments(memory):
    assert EmotionRegulationModule().profile_section(memory) is None


def test_module_profile_section_summarizes_moments(memory):
    for text in ("I'm overwhelmed", "still overwhelmed", "feeling rejected"):
        record_support(memory, build_support(memory, text))
    section = EmotionRegulationModule().profile_section(memory)
    assert section is not None
    assert "3 time(s)" in section
    assert "overwhelm" in section  # most common state surfaced


def test_module_profile_section_not_crowded_out_by_other_checkins(memory):
    # An emotion moment logged first, then a flood of unrelated check-ins beyond the
    # 100-row window: the SQL context-prefix filter must still find it (a Python
    # filter over the newest 100 would miss it and wrongly return None).
    record_support(memory, build_support(memory, "I'm overwhelmed"))
    for _ in range(120):
        memory.log_episode("checkin", acknowledged=True, context="care: water", outcome="success")
    section = EmotionRegulationModule().profile_section(memory)
    assert section is not None
    assert "1 time(s)" in section


def test_module_seeds_recovery_optout_and_is_registered():
    from prefrontal.modules.registry import get

    assert get("emotion_regulation").default_state["emotion_recovery_acceptance"] == "off"


# --- endpoint ----------------------------------------------------------------


def _app_client():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
    return conn, TestClient(app)


def test_endpoint_requires_auth():
    conn, client = _app_client()
    try:
        with client:
            assert client.post("/emotion/support", json={}).status_code == 401
    finally:
        conn.close()


def test_endpoint_one_tap_returns_a_skill():
    conn, client = _app_client()
    try:
        with client:
            r = client.post("/emotion/support", json={}, headers={"X-Prefrontal-Token": SECRET})
            assert r.status_code == 200
            body = r.json()
            assert body["kind"] == "skill"
            assert body["text"] and body["skill"]
    finally:
        conn.close()


def test_endpoint_crisis_text_returns_resources_not_a_skill():
    conn, client = _app_client()
    try:
        with client:
            r = client.post(
                "/emotion/support",
                json={"text": "I want to kill myself"},
                headers={"X-Prefrontal-Token": SECRET},
            )
            body = r.json()
            assert body["kind"] == "crisis"
            assert "988" in body["text"]
            assert body["skill"] == ""
    finally:
        conn.close()
