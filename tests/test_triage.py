"""Tests for the triage classifier core (prefrontal/triage.py).

Two layers, per the spec: the deterministic heuristic (the contract — must pass
with no model) and the LLM refinement path (a fake generator proving coercion +
fallback). Nothing here touches a store; the apply/wiring slices come later.
"""

from __future__ import annotations

from datetime import date

from prefrontal.integrations import OllamaError
from prefrontal.triage import ROUTE_FOR_KIND, Signal, classify

TODAY = date(2026, 7, 3)  # a Friday, for deterministic weekday math


def mail(title, body="", sender="", meta=None):
    return Signal(source="mail", title=title, body=body, sender=sender, meta=meta or {})


class FakeGen:
    """Records calls and returns a canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate(self, prompt, *, system=None):
        self.calls += 1
        return self.reply


class RaisingGen:
    def generate(self, prompt, *, system=None):
        raise OllamaError("model down")


# --- heuristic contract (model off) ------------------------------------------


def test_dentist_confirmation_is_a_commitment_with_a_date():
    d = classify(mail("Dentist appointment confirmed for Tuesday 3pm"), today=TODAY)
    assert (d.kind, d.route, d.source) == ("commitment", "commitment", "heuristic")
    assert d.fields["when"] == "2026-07-07"  # next Tuesday


def test_overdue_invoice_is_an_urgent_action():
    d = classify(mail("Re: overdue invoice — please pay today"), today=TODAY)
    assert (d.kind, d.urgency, d.route) == ("action", "now", "todo")


def test_newsletter_is_noise_dropped():
    d = classify(mail("This week's newsletter: 20% off everything",
                       sender="newsletter@shop.com"), today=TODAY)
    assert (d.kind, d.urgency, d.route) == ("noise", "none", "drop")
    assert d.reason  # never silent


def test_first_person_report_is_an_outcome():
    d = classify(mail("I left at 9:15 and made it on time"), today=TODAY)
    assert (d.kind, d.route) == ("outcome", "episode")


def test_coaching_directive_is_a_preference():
    # "please" would read as an action, but the preference rule wins first.
    d = classify(mail("Please stop texting me after 3pm"), today=TODAY)
    assert (d.kind, d.route) == ("preference", "state")


def test_undated_note_falls_through_to_info():
    d = classify(mail("thoughts on the garden"), today=TODAY)
    assert (d.kind, d.route) == ("info", "surface")


def test_route_always_follows_kind():
    for sig in [
        mail("Dentist appointment confirmed Tuesday 3pm"),
        mail("please pay the overdue invoice today"),
        mail("newsletter: 20% off", sender="noreply@x.com"),
        mail("I made it on time"),
        mail("i prefer morning reminders"),
        mail("random musings"),
    ]:
        d = classify(sig, today=TODAY)
        assert d.route == ROUTE_FOR_KIND[d.kind]


def test_list_header_meta_marks_noise():
    d = classify(mail("Big announcement", sender="team@company.com",
                      meta={"list-unsubscribe": "<https://x/unsub>"}), today=TODAY)
    assert d.kind == "noise" and d.confidence >= 0.9


# --- LLM refinement path (fake generator) ------------------------------------


def test_high_confidence_heuristic_skips_the_model():
    gen = FakeGen('{"kind":"action","urgency":"now"}')
    d = classify(mail("newsletter: 20% off", sender="noreply@x.com"), client=gen, today=TODAY)
    assert gen.calls == 0  # 0.9 ≥ HEURISTIC_TRUST → cheap path, no model call
    assert d.source == "heuristic"


def test_ambiguous_signal_is_refined_by_the_model():
    # "follow up on the thing" → a low-confidence action (0.6); the model is consulted.
    gen = FakeGen('{"kind":"noise","urgency":"none","reason":"promo blast"}')
    d = classify(mail("follow up on the thing"), client=gen, today=TODAY)
    assert gen.calls == 1
    assert (d.kind, d.source, d.reason) == ("noise", "llm", "promo blast")


def test_malformed_model_json_falls_back_to_heuristic():
    gen = FakeGen("sorry, I can't do that")
    d = classify(mail("follow up on the thing"), client=gen, today=TODAY)
    assert d.source == "heuristic" and d.kind == "action"


def test_unknown_kind_from_model_falls_back():
    gen = FakeGen('{"kind":"banana","urgency":"now"}')
    d = classify(mail("follow up on the thing"), client=gen, today=TODAY)
    assert d.source == "heuristic"


def test_model_error_falls_back_to_heuristic():
    d = classify(mail("follow up on the thing"), client=RaisingGen(), today=TODAY)
    assert d.source == "heuristic" and d.kind == "action"


# --- drop threshold (surface low-confidence noise) ---------------------------


def test_low_confidence_noise_is_surfaced_when_threshold_set():
    sig = mail("Weekly summary digest", sender="team@work.com")  # subject-cue noise, 0.78
    assert classify(sig, today=TODAY).route == "drop"  # default: dropped
    surfaced = classify(sig, today=TODAY, drop_threshold=0.8)
    assert surfaced.kind == "noise" and surfaced.route == "surface"
