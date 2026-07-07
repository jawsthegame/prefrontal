"""Tests for the self/FYI commitment classifier."""

from __future__ import annotations

from prefrontal.classify import (
    build_system_prompt,
    classify_kind,
    parse_kind_reply,
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
    assert parse_kind_reply("  This looks like FYI to me.") == "fyi"
    assert parse_kind_reply("That's the user's own (self) event") == "self"
    assert parse_kind_reply("Looks like a CHILD appointment") == "child"
    # Earliest label wins when more than one appears.
    assert parse_kind_reply("child, not self") == "child"
    assert parse_kind_reply("") is None
    assert parse_kind_reply("maybe?") is None


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
    assert classify_kind("Standup", client=_StubClient("SELF"), child_names=["Sam"]) == ("self", "llm")


def test_classify_kind_model_can_return_child():
    assert classify_kind("Pediatrician", client=_StubClient("CHILD")) == ("child", "llm")


def test_build_system_prompt_folds_in_examples():
    base = build_system_prompt([])
    assert "SELF" in base and "FYI" in base and "CHILD" in base
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
