"""Tests for ambiguity clarification (task-initiation lever).

Covers the pure ambiguity heuristic and its gate, the LLM-first-with-heuristic
detector (including the honest no-model fallback), the playbook registry + free-
text → task-type mapping, and the clarifications store round-trip
(pending → resolved/dismissed, and the "never re-ask" target set).
"""

from __future__ import annotations

import httpx
import pytest

from prefrontal.clarify import (
    AMBIGUITY_THRESHOLD,
    MAX_OPTIONS,
    ambiguity_score,
    ambiguous_token,
    candidate_view,
    detect_clarification,
    is_ambiguous,
    known_task_types,
    playbook_view,
    resolve_playbook,
)
from prefrontal.clarify import _known_task_type as infer_task_type
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.store import MemoryStore
from tests.conftest import scoped_default


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _client(reply: str, *, status: int = 200) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"response": reply})

    return OllamaClient(transport=httpx.MockTransport(handler))


# -- ambiguity heuristic ------------------------------------------------------


def test_bare_noun_is_ambiguous():
    assert ambiguity_score("Tax") >= AMBIGUITY_THRESHOLD
    assert is_ambiguous("Tax")
    assert is_ambiguous("Mom")
    assert is_ambiguous("Passport")


def test_spelled_out_action_is_not_ambiguous():
    # An action verb + object names the move — no honing needed.
    assert not is_ambiguous("Call the dentist to reschedule the cleaning")
    assert not is_ambiguous("Pay the electric bill online")
    # A concrete detail (a time/amount) also pins a short title down.
    assert not is_ambiguous("Tax return by April 15")


def test_empty_and_blank_score_zero():
    assert ambiguity_score("") == 0.0
    assert ambiguity_score("   ") == 0.0
    assert not is_ambiguous("")


def test_ambiguous_token_detection():
    assert ambiguous_token("Tax") == "tax"
    assert ambiguous_token("annual car thing") == "car"
    assert ambiguous_token("write the quarterly report") == "quarterly"
    assert ambiguous_token("buy milk and eggs") is None


# -- detector: heuristic fallback + LLM path ----------------------------------


def test_detect_returns_none_for_clear_title():
    assert detect_clarification("Call the plumber back at 3pm") is None


def test_detect_heuristic_uses_known_interpretations_offline():
    # No client → hand-authored readings; "Tax" maps its first reading to a playbook.
    cand = detect_clarification("Tax")
    assert cand is not None and cand.source == "heuristic"
    assert cand.options[0].task_type == "tax_filing"
    # Always ends with a "Something else" escape hatch, and is option-capped.
    assert cand.options[-1].label == "Something else"
    assert len(cand.options) <= MAX_OPTIONS


def test_detect_generic_heuristic_for_unmapped_token():
    cand = detect_clarification("Project")
    assert cand is not None
    labels = [o.label for o in cand.options]
    assert "Something else" in labels
    # No hand-authored readings for "project" → generic is-it-a-task question.
    assert all(o.task_type is None for o in cand.options)


def test_detect_uses_model_when_available_and_maps_task_type():
    reply = (
        '{"question": "What kind of tax thing is this?", '
        '"options": ["File my tax return", "Pay a property tax bill", "See the accountant"]}'
    )
    cand = detect_clarification("Taxes", client=_client(reply))
    assert cand is not None and cand.source == "llm"
    assert cand.question == "What kind of tax thing is this?"
    # "File my tax return" is keyword-mapped onto the tax_filing playbook.
    assert cand.options[0].task_type == "tax_filing"
    assert cand.options[-1].label == "Something else"


def test_detect_falls_back_to_heuristic_on_model_error():
    cand = detect_clarification("Tax", client=_client("", status=500))
    assert cand is not None and cand.source == "heuristic"


def test_detect_falls_back_when_model_returns_no_options():
    cand = detect_clarification("Tax", client=_client('{"question": "hm", "options": []}'))
    assert cand is not None and cand.source == "heuristic"


# -- playbooks ----------------------------------------------------------------


def test_resolve_playbook_known_and_unknown():
    pb = resolve_playbook("tax_filing")
    assert pb is not None and pb.steps and pb.title
    assert resolve_playbook("nope") is None
    assert resolve_playbook(None) is None


def test_known_task_types_all_resolve():
    for tt in known_task_types():
        assert resolve_playbook(tt) is not None


def test_infer_task_type_from_free_text():
    assert infer_task_type("I need to file my tax return") == "tax_filing"
    assert infer_task_type("renew my passport") == "passport_renewal"
    assert infer_task_type("dentist appointment") == "medical_appointment"
    assert infer_task_type("something totally unrelated") is None


def test_views_are_json_ready():
    cand = detect_clarification("Tax")
    view = candidate_view(cand)
    assert view["title"] == "Tax" and view["options"][0]["task_type"] == "tax_filing"
    pv = playbook_view(resolve_playbook("tax_filing"))
    assert pv["task_type"] == "tax_filing" and isinstance(pv["steps"], list)
    assert all("title" in s for s in pv["steps"])


# -- store round-trip ---------------------------------------------------------


def test_clarification_store_lifecycle(store):
    tid = store.add_todo("Tax", priority=2)
    cid = store.add_clarification(
        target_type="todo",
        target_id=tid,
        title="Tax",
        question="Which is it?",
        options=[{"label": "File return", "task_type": "tax_filing"}, {"label": "Something else"}],
        source="heuristic",
    )
    pending = store.list_clarifications("pending")
    assert len(pending) == 1 and pending[0]["options"][0]["task_type"] == "tax_filing"
    # Any clarification history means the sweep won't re-ask this item.
    assert store.clarified_target_ids("todo") == {tid}

    assert store.resolve_clarification(cid, answer="File return", task_type="tax_filing")
    # Idempotent: a resolved row doesn't move again.
    assert not store.resolve_clarification(cid, answer="x", task_type=None)
    resolved = store.list_clarifications("resolved")
    assert resolved[0]["answer"] == "File return" and resolved[0]["task_type"] == "tax_filing"
    assert store.clarified_target_ids("todo") == {tid}  # still remembered


def test_dismiss_marks_not_ambiguous(store):
    cid = store.add_clarification(
        target_type="commitment", target_id=7, title="Block",
        question="?", options=[{"label": "x"}],
    )
    assert store.dismiss_clarification(cid)
    assert not store.dismiss_clarification(cid)  # idempotent
    assert store.list_clarifications("dismissed")[0]["id"] == cid
    assert store.clarified_target_ids("commitment") == {7}


def test_set_todo_notes_round_trip(store):
    tid = store.add_todo("Tax")
    assert store.set_todo_notes(tid, "Clarified: Filing my tax return")
    assert store.get_todo(tid)["notes"] == "Clarified: Filing my tax return"


def test_pending_unique_per_item(store):
    """At most one pending question per item (the partial unique index)."""
    import sqlite3

    store.add_clarification(
        target_type="todo", target_id=5, title="Tax", question="?",
        options=[{"label": "x"}],
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.add_clarification(
            target_type="todo", target_id=5, title="Tax", question="again?",
            options=[{"label": "y"}],
        )
