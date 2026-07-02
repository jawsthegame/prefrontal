"""Tests for the dashboard assistant: parsing, validation, execution, endpoints.

The assistant turns a natural-language message into a validated action list and
(on Apply) executes it against the *scoped* store. These tests never touch a real
model — a :class:`_FakeClient` returns canned JSON so behavior is deterministic —
and cover the security-relevant invariants: only whitelisted ops survive, ids are
resolved against the user's own data, and a stale id changes nothing.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prefrontal import assistant
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

SECRET = "assistant-secret"


class _FakeClient:
    """A model stand-in: available, returns a fixed reply (or raises).

    ``reply`` is returned verbatim from :meth:`generate` (ignoring the prompt),
    so a test controls exactly what "the model said". ``error`` makes
    :meth:`generate` raise, simulating a down/misconfigured backend.
    """

    def __init__(self, reply: str = "", *, error: bool = False) -> None:
        self._reply = reply
        self._error = error
        self.available_result = True

    def available(self) -> bool:
        return self.available_result

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if self._error:
            raise OllamaError("down")
        return self._reply


# --- JSON extraction ------------------------------------------------------


# JSON extraction moved to prefrontal.llm_json (shared with the todo augmenter /
# decomposer); see tests/test_llm_json.py.


# --- interpret ------------------------------------------------------------


def test_interpret_parses_reply_and_actions():
    client = _FakeClient(
        json.dumps({"reply": "Done", "actions": [{"op": "drop_todo", "todo_id": 3}]})
    )
    reply, raw = assistant.interpret("drop it", {}, client=client)
    assert reply == "Done"
    assert raw == [{"op": "drop_todo", "todo_id": 3}]


def test_interpret_model_down_degrades_gracefully():
    reply, raw = assistant.interpret("x", {}, client=_FakeClient(error=True))
    assert reply == ""
    assert raw == []


def test_interpret_unparseable_reply_yields_empty():
    reply, raw = assistant.interpret("x", {}, client=_FakeClient("no json here"))
    assert (reply, raw) == ("", [])


# --- validation -----------------------------------------------------------


@pytest.fixture()
def snapshot():
    return {
        "todos": [
            {"id": 4, "title": "Call dentist", "priority": 1},
            {"id": 7, "title": "File taxes", "priority": 2},
        ],
        "commitments": [{"id": 2, "title": "Standup", "start_at": "2026-07-02 14:00:00"}],
        "conflicts": [{"key": "busy::dentist", "label": "Busy vs Dentist"}],
    }


def test_validate_rejects_unknown_op(snapshot):
    actions, errors = assistant.validate_actions([{"op": "delete_everything"}], snapshot)
    assert actions == []
    assert any("unsupported" in e for e in errors)


def test_validate_rejects_hallucinated_todo_id(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "complete_todo", "todo_id": 999}], snapshot
    )
    assert actions == []
    assert any("999" in e for e in errors)


def test_validate_resolves_real_todo(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "set_priority", "todo_id": 4, "priority": 3}], snapshot
    )
    assert errors == []
    assert actions[0].op == "set_priority"
    assert actions[0].params == {"todo_id": 4, "priority": 3}
    assert "urgent" in actions[0].summary


def test_validate_priority_out_of_range(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "set_priority", "todo_id": 4, "priority": 9}], snapshot
    )
    assert any("0" in e for e in errors)


def test_validate_add_todo_needs_title(snapshot):
    _actions, errors = assistant.validate_actions([{"op": "add_todo", "title": "  "}], snapshot)
    assert errors


def test_validate_dismiss_conflict_unknown_key(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "dismiss_conflict", "key": "nope"}], snapshot
    )
    assert errors


def test_validate_dismiss_conflict_known_key(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "dismiss_conflict", "key": "busy::dentist"}], snapshot
    )
    assert errors == []
    assert actions[0].op == "dismiss_conflict"


def test_wire_roundtrip_revalidates(snapshot):
    """Wire-format actions echoed by the client re-validate identically."""
    actions, _ = assistant.validate_actions(
        [{"op": "rename_todo", "todo_id": 7, "title": "Do taxes"}], snapshot
    )
    wire = [a.to_wire() for a in actions]
    again, errors = assistant.validate_actions(wire, snapshot)
    assert errors == []
    assert again[0].params == {"todo_id": 7, "title": "Do taxes"}


# --- store setters + execution -------------------------------------------


@pytest.fixture()
def memory():
    conn = init_db(":memory:")
    store = MemoryStore(conn)
    scoped = scoped_default(store)
    try:
        yield scoped
    finally:
        conn.close()


def test_store_setters_open_todo(memory):
    tid = memory.add_todo("Call dentist", priority=1, estimate_minutes=15.0)
    assert memory.set_todo_priority(tid, 3) is True
    assert memory.set_todo_estimate(tid, 45.0) is True
    assert memory.set_todo_title(tid, "Call the dentist back") is True
    row = memory.get_todo(tid)
    assert row["priority"] == 3
    assert row["estimate_minutes"] == 45.0
    assert row["title"] == "Call the dentist back"


def test_store_setters_noop_on_closed_todo(memory):
    tid = memory.add_todo("Done thing")
    memory.close_todo(tid, "done")
    assert memory.set_todo_priority(tid, 3) is False
    assert memory.set_todo_title(tid, "x") is False


def test_store_setters_noop_on_absent_todo(memory):
    assert memory.set_todo_estimate(999, 10.0) is False


def _plan_and_execute(memory, raw_actions, tz="UTC"):
    """Validate raw actions against the live store, then execute them."""
    snap = assistant.build_snapshot(memory)
    actions, _errors = assistant.validate_actions(raw_actions, snap)
    return assistant.execute_actions(memory, actions, timezone=tz)


def test_execute_add_todo(memory):
    results = _plan_and_execute(
        memory, [{"op": "add_todo", "title": "Email Sam", "priority": 2}]
    )
    assert results[0]["ok"] is True
    titles = [t["title"] for t in memory.open_todos()]
    assert "Email Sam" in titles


def test_execute_set_priority_and_complete(memory):
    tid = memory.add_todo("Call dentist", priority=1)
    results = _plan_and_execute(
        memory,
        [
            {"op": "set_priority", "todo_id": tid, "priority": 3},
            {"op": "complete_todo", "todo_id": tid},
        ],
    )
    # Both actions resolved against the snapshot taken before execution.
    assert [r["ok"] for r in results] == [True, True]
    assert memory.get_todo(tid)["status"] == "done"


def test_execute_set_deadline_converts_tz(memory):
    tid = memory.add_todo("Ship report")
    results = _plan_and_execute(
        memory, [{"op": "set_deadline", "todo_id": tid, "deadline": "2026-07-10"}], tz="UTC"
    )
    assert results[0]["ok"] is True
    assert memory.get_todo(tid)["deadline"].startswith("2026-07-10")


def test_execute_add_and_cancel_commitment(memory):
    add = _plan_and_execute(
        memory,
        [{"op": "add_commitment", "title": "Lunch", "start_at": "2099-01-01 12:00"}],
    )
    assert add[0]["ok"] is True
    upcoming = memory.upcoming_commitments()
    assert upcoming and upcoming[0]["title"] == "Lunch"
    cid = upcoming[0]["id"]
    cancel = _plan_and_execute(memory, [{"op": "cancel_commitment", "commitment_id": cid}])
    assert cancel[0]["ok"] is True
    assert all(c["id"] != cid for c in memory.upcoming_commitments())


def test_execute_bad_date_reports_per_action(memory):
    tid = memory.add_todo("Thing")
    # A validated action whose date can't be parsed at execution time fails
    # softly (per-action detail) rather than raising.
    action = assistant.ValidatedAction(
        "set_deadline", {"todo_id": tid, "deadline": "not-a-date"}, "…"
    )
    results = assistant.execute_actions(memory, [action], timezone="UTC")
    assert results[0]["ok"] is False
    assert "couldn't apply" in results[0]["detail"]


# --- endpoints ------------------------------------------------------------


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


def _client(store, fake):
    app = create_app(store=store, settings=Settings(), anthropic=fake)
    return TestClient(app)


def test_assistant_requires_auth(store):
    with _client(store, _FakeClient("{}")) as c:
        assert c.post("/assistant", json={"message": "hi"}).status_code == 401


def test_assistant_proposes_without_writing(store, user_store):
    tid = user_store.add_todo("Call dentist", priority=1)
    fake = _FakeClient(
        json.dumps(
            {"reply": "Sure", "actions": [{"op": "set_priority", "todo_id": tid, "priority": 3}]}
        )
    )
    with _client(store, fake) as c:
        resp = c.post(
            "/assistant",
            json={"message": "make the dentist call urgent"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "anthropic"
    assert len(body["actions"]) == 1
    assert body["actions"][0]["op"] == "set_priority"
    # Propose does NOT write.
    assert user_store.get_todo(tid)["priority"] == 1


def test_assistant_apply_executes(store, user_store):
    tid = user_store.add_todo("Call dentist", priority=1)
    fake = _FakeClient(
        json.dumps({"reply": "", "actions": [{"op": "complete_todo", "todo_id": tid}]})
    )
    with _client(store, fake) as c:
        proposed = c.post(
            "/assistant",
            json={"message": "mark the dentist call done"},
            headers={"X-Prefrontal-Token": SECRET},
        ).json()
        applied = c.post(
            "/assistant/apply",
            json={"actions": proposed["actions"]},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert applied.status_code == 200
    body = applied.json()
    assert body["applied"] == 1
    assert body["results"][0]["ok"] is True
    assert user_store.get_todo(tid)["status"] == "done"


def test_assistant_apply_rejects_tampered_id(store, user_store):
    """An action for an id that isn't the caller's is dropped, not executed."""
    with _client(store, _FakeClient("{}")) as c:
        resp = c.post(
            "/assistant/apply",
            json={"actions": [{"op": "complete_todo", "todo_id": 4242}]},
            headers={"X-Prefrontal-Token": SECRET},
        )
    body = resp.json()
    assert body["applied"] == 0
    assert body["results"] == []
    assert body["errors"]


def test_assistant_falls_back_to_ollama_when_anthropic_absent(store):
    """With no Claude key, the endpoint uses the (offline) local model and 200s."""
    fake = _FakeClient("{}")
    fake.available_result = False  # simulate "no Anthropic key configured"
    with _client(store, fake) as c:
        resp = c.post(
            "/assistant",
            json={"message": "hi"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 200
    # conftest forces the default Ollama client offline → no actions, graceful reply.
    body = resp.json()
    assert body["provider"] == "ollama"
    assert body["actions"] == []
