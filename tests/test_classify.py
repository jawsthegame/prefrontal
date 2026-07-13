"""Tests for the self/FYI commitment classifier."""

from __future__ import annotations

from prefrontal.classify import (
    build_system_prompt,
    classify_kind,
    parse_kind_reply,
    roster_care_match,
    roster_child_match,
)
from prefrontal.integrations.ollama import OllamaError


class _StubClient:
    """An Ollama-like client returning canned replies (or raising)."""

    def __init__(self, reply: str | None = None, *, error: bool = False) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        if self.error:
            raise OllamaError("down")
        assert self.reply is not None
        return self.reply


def test_parse_kind_reply_reads_any_label():
    assert parse_kind_reply("FYI") == "fyi"
    assert parse_kind_reply("SELF") == "self"
    assert parse_kind_reply("CHILD") == "child"
    assert parse_kind_reply("CARE") == "care"
    assert parse_kind_reply("  This looks like FYI to me.") == "fyi"
    assert parse_kind_reply("That's the user's own (self) event") == "self"
    assert parse_kind_reply("Looks like a CHILD appointment") == "child"
    # Earliest label wins when more than one appears.
    assert parse_kind_reply("child, not self") == "child"
    assert parse_kind_reply("") is None
    assert parse_kind_reply("maybe?") is None


def test_parse_kind_reply_matches_whole_words_not_fragments():
    """A label embedded in an unrelated word must not match (word boundaries)."""
    # 'self' inside 'yourself' and 'child' inside 'children' are not labels.
    assert parse_kind_reply("something you'd handle yourself") is None
    assert parse_kind_reply("bring the children along") is None
    # The bug this guards: a clear FYI reply that merely contains 'yourself'
    # used to match 'self' first and misread as self.
    assert parse_kind_reply("It's FYI — not something you'd attend yourself") == "fyi"


def test_roster_child_match_word_boundary_and_case():
    assert roster_child_match("Sam — dentist", ["Sam"]) is True
    assert roster_child_match("SAM dentist", ["Sam"]) is True          # case-insensitive
    assert roster_child_match("Sam's checkup", ["Sam"]) is True        # possessive
    assert roster_child_match("Samuelson meeting", ["Sam"]) is False   # not a substring
    assert roster_child_match("team standup", ["Sam"]) is False
    assert roster_child_match("dentist", ["Sam", "Ada"]) is False
    assert roster_child_match("Sam dentist", []) is False              # no roster
    assert roster_child_match("", ["Sam"]) is False


def test_classify_kind_roster_pass_wins_offline_and_over_model():
    # No model at all, but the title names a kid → deterministic 'child'.
    assert classify_kind("Sam dentist", child_names=["Sam"]) == ("child", "roster")
    # Roster short-circuits before the model is even consulted.
    client = _StubClient("SELF")
    assert classify_kind("Sam dentist", client=client, child_names=["Sam"]) == ("child", "roster")
    assert client.calls == []
    # No roster match → the model is still consulted as before.
    assert classify_kind(
        "Standup", client=_StubClient("SELF"), child_names=["Sam"]
    ) == ("self", "llm")


def test_roster_care_match_word_boundary_and_case():
    assert roster_care_match("Mom — cardiology", ["Mom"]) is True
    assert roster_care_match("MOM's PT", ["Mom"]) is True              # case + possessive
    assert roster_care_match("Mommy group", ["Mom"]) is False         # not a substring
    assert roster_care_match("cardiology", ["Mom", "Dad"]) is False
    assert roster_care_match("Mom appt", []) is False                 # no roster


def test_classify_kind_care_roster_offline_and_over_model():
    # Names a care recipient, no model → deterministic 'care'.
    assert classify_kind("Mom cardiology", care_names=["Mom"]) == ("care", "roster")
    # Care roster short-circuits the model too.
    client = _StubClient("SELF")
    assert classify_kind("Mom PT", client=client, care_names=["Mom"]) == ("care", "roster")
    assert client.calls == []


def test_classify_kind_child_roster_wins_over_care_when_name_on_both():
    # A name on both lists resolves as 'child' — the child pass runs first.
    assert classify_kind(
        "Alex checkup", child_names=["Alex"], care_names=["Alex"]
    ) == ("child", "roster")


def test_classify_kind_model_can_return_child():
    assert classify_kind("Pediatrician", client=_StubClient("CHILD")) == ("child", "llm")


def test_classify_kind_model_can_return_care():
    # An adult care-recipient's appointment (aging parent / ill partner).
    assert classify_kind("Mom's cardiology", client=_StubClient("CARE")) == ("care", "llm")


def test_build_system_prompt_folds_in_examples():
    base = build_system_prompt([])
    assert "SELF" in base and "FYI" in base and "CHILD" in base and "CARE" in base
    evolved = build_system_prompt(
        [{"display": "Harlequin Brow Appt", "kind": "fyi"},
         {"display": "Sam dentist", "kind": "child"}]
    )
    assert "Harlequin Brow Appt" in evolved
    assert "Sam dentist" in evolved and "CHILD" in evolved
    assert len(evolved) > len(base)


def test_classify_kind_uses_model_verdict():
    client = _StubClient("FYI")
    assert classify_kind("Harlequin Brow Appt", client=client) == ("fyi", "llm")
    assert client.calls, "the model should have been consulted"


def test_classify_kind_passes_examples_into_the_prompt():
    client = _StubClient("FYI")
    classify_kind(
        "Brow appt", client=client, examples=[{"display": "Nails", "kind": "fyi"}]
    )
    _, system = client.calls[0]
    assert system and "Nails" in system


def test_classify_kind_falls_back_to_self():
    # Model unreachable, model gibberish, and no model at all → conservative self.
    assert classify_kind("X", client=_StubClient(error=True)) == ("self", "default")
    assert classify_kind("X", client=_StubClient("dunno")) == ("self", "default")
    assert classify_kind("X", client=None) == ("self", "default")
    assert classify_kind("   ", client=_StubClient("FYI")) == ("self", "default")
