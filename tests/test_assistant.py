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


class _CapturingClient(_FakeClient):
    """Like :class:`_FakeClient`, but records the prompt it was handed."""

    def __init__(self, reply: str = "") -> None:
        super().__init__(reply)
        self.prompt: str | None = None

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.prompt = prompt
        return super().generate(prompt, system=system)


def test_interpret_anchors_prompt_to_local_now_and_timezone():
    """The prompt names the user's *local* now + zone so relative times resolve.

    Without this the model has no "now" and no zone, guesses a date, and often
    emits a UTC-assumed time — the bug where a "3pm" edit lands hours off.
    """
    from datetime import datetime

    client = _CapturingClient(json.dumps({"reply": "ok", "actions": []}))
    # 2026-07-07 03:30 UTC is 2026-07-06 23:30 EDT.
    assistant.interpret(
        "add lunch tomorrow at noon", {}, client=client,
        now=datetime(2026, 7, 7, 3, 30, 0), tz="America/New_York",
    )
    assert client.prompt is not None
    assert "America/New_York" in client.prompt
    # The *local* wall clock, not the UTC 03:30.
    assert "2026-07-06 23:30" in client.prompt
    assert "03:30" not in client.prompt
    assert "LOCAL" in client.prompt


def test_interpret_unparseable_reply_yields_empty():
    reply, raw = assistant.interpret("x", {}, client=_FakeClient("no json here"))
    assert (reply, raw) == ("", [])


# --- plan (reply honesty) -------------------------------------------------


def test_plan_suppresses_confident_reply_when_all_actions_dropped(memory):
    """A confident model reply is replaced by the honest fallback when every
    proposed edit fails validation — otherwise the user is told an edit is
    coming that will never happen ("said it would update a todo, but didn't")."""
    memory.add_todo("Call dentist", priority=1)  # id 1; model targets a bogus id
    client = _FakeClient(
        json.dumps(
            {
                "reply": "Done — I've bumped the dentist call to urgent.",
                "actions": [{"op": "set_priority", "todo_id": 999, "priority": 3}],
            }
        )
    )
    result = assistant.plan("make the dentist call urgent", memory, client=client)
    assert result.actions == []
    assert result.errors  # the dropped action is reported
    assert "Done" not in result.reply
    assert result.reply == "I couldn't turn that into an edit I can make."


def test_plan_keeps_informational_reply_with_no_actions(memory):
    """A reply with no actions *and no errors* is a legitimate answer (e.g. the
    user asked a question) — it must not be clobbered by the fallback."""
    memory.add_todo("Call dentist", priority=1)
    client = _FakeClient(
        json.dumps({"reply": "You have 1 open todo: call the dentist.", "actions": []})
    )
    result = assistant.plan("what's on my plate?", memory, client=client)
    assert result.actions == []
    assert result.errors == []
    assert result.reply == "You have 1 open todo: call the dentist."


def test_plan_keeps_reply_when_actions_are_valid(memory):
    """When the model proposes a valid edit, its reply is preserved verbatim."""
    tid = memory.add_todo("Call dentist", priority=1)
    client = _FakeClient(
        json.dumps(
            {
                "reply": "I'll bump the dentist call to urgent.",
                "actions": [{"op": "set_priority", "todo_id": tid, "priority": 3}],
            }
        )
    )
    result = assistant.plan("make the dentist call urgent", memory, client=client)
    assert len(result.actions) == 1
    assert result.reply == "I'll bump the dentist call to urgent."


# --- validation -----------------------------------------------------------


@pytest.fixture()
def snapshot():
    return {
        "todos": [
            {"id": 4, "title": "Call dentist", "priority": 1},
            {"id": 7, "title": "File taxes", "priority": 2},
        ],
        "commitments": [{"id": 2, "title": "Standup", "start_at": "2026-07-02 14:00:00"}],
        "outings": [
            {"id": 3, "intention": "Coffee run", "time_window_minutes": 15.0,
             "status": "active", "start_at": "2026-07-02 14:00:00"},
            {"id": 9, "intention": "Groceries", "time_window_minutes": 45.0,
             "status": "returned", "start_at": "2026-07-01 10:00:00"},
        ],
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


def test_validate_rename_outing_resolves(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "rename_outing", "outing_id": 3, "intention": "Grocery run"}], snapshot
    )
    assert errors == []
    assert actions[0].params == {"outing_id": 3, "intention": "Grocery run"}
    assert "Grocery run" in actions[0].summary


def test_validate_set_outing_window_resolves(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "set_outing_window", "outing_id": 3, "time_window_minutes": 30}], snapshot
    )
    assert errors == []
    assert actions[0].params == {"outing_id": 3, "time_window_minutes": 30.0}


def test_validate_outing_ops_target_past_outings(snapshot):
    """Editing works on a already-returned outing too, not just the active one."""
    actions, errors = assistant.validate_actions(
        [{"op": "rename_outing", "outing_id": 9, "intention": "Weekly shop"}], snapshot
    )
    assert errors == []
    assert actions[0].params["outing_id"] == 9


def test_validate_set_outing_start_resolves(snapshot):
    actions, errors = assistant.validate_actions(
        [{"op": "set_outing_start", "outing_id": 3, "start_at": "2026-07-02 13:30"}],
        snapshot,
    )
    assert errors == []
    assert actions[0].params == {"outing_id": 3, "start_at": "2026-07-02 13:30"}
    assert "start" in actions[0].summary


def test_validate_set_outing_start_needs_value(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "set_outing_start", "outing_id": 3, "start_at": "  "}], snapshot
    )
    assert errors


def test_validate_outing_unknown_id(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "set_outing_window", "outing_id": 999, "time_window_minutes": 20}], snapshot
    )
    assert any("999" in e for e in errors)


def test_validate_outing_window_out_of_range(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "set_outing_window", "outing_id": 3, "time_window_minutes": 99999}], snapshot
    )
    assert errors


def test_validate_rename_outing_needs_intention(snapshot):
    _actions, errors = assistant.validate_actions(
        [{"op": "rename_outing", "outing_id": 3, "intention": "  "}], snapshot
    )
    assert errors


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


def test_store_setters_outing(memory):
    oid = memory.start_outing("Coffee run", 15.0)
    assert memory.set_outing_intention(oid, "Grocery run")["intention"] == "Grocery run"
    assert memory.set_outing_window(oid, 40.0)["time_window_minutes"] == 40.0
    row = memory.get_outing(oid)
    assert row["intention"] == "Grocery run"
    assert row["time_window_minutes"] == 40.0


def test_store_setters_outing_edit_after_close(memory):
    """A returned outing is still editable (a retroactive correction)."""
    oid = memory.start_outing("Coffee run", 15.0)
    memory.close_outing(oid, "returned")
    assert memory.set_outing_intention(oid, "Espresso run") is not None
    assert memory.get_outing(oid)["intention"] == "Espresso run"


def test_store_setters_noop_on_absent_outing(memory):
    assert memory.set_outing_intention(999, "x") is None
    assert memory.set_outing_window(999, 20.0) is None
    assert memory.set_outing_departure(999, "2026-07-02 14:00:00") is None


def test_store_setter_outing_departure(memory):
    oid = memory.start_outing("Coffee run", 15.0, departure_at="2026-07-02 14:15:00")
    updated = memory.set_outing_departure(oid, "2026-07-02 14:00:00")
    assert updated["departure_at"].startswith("2026-07-02 14:00")
    assert memory.get_outing(oid)["departure_at"].startswith("2026-07-02 14:00")


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


def test_execute_rename_and_adjust_outing(memory):
    oid = memory.start_outing("Coffee run", 15.0)
    results = _plan_and_execute(
        memory,
        [
            {"op": "rename_outing", "outing_id": oid, "intention": "Grocery run"},
            {"op": "set_outing_window", "outing_id": oid, "time_window_minutes": 30},
        ],
    )
    assert [r["ok"] for r in results] == [True, True]
    row = memory.get_outing(oid)
    assert row["intention"] == "Grocery run"
    assert row["time_window_minutes"] == 30.0


def test_execute_set_outing_start_moves_departure(memory):
    oid = memory.start_outing("Coffee run", 15.0, departure_at="2026-07-02 14:15:00")
    results = _plan_and_execute(
        memory,
        [{"op": "set_outing_start", "outing_id": oid, "start_at": "2026-07-02 14:00"}],
        tz="UTC",
    )
    assert results[0]["ok"] is True
    assert memory.get_outing(oid)["departure_at"].startswith("2026-07-02 14:00")


def test_execute_set_outing_start_bad_date_reports_softly(memory):
    oid = memory.start_outing("Coffee run", 15.0)
    action = assistant.ValidatedAction(
        "set_outing_start", {"outing_id": oid, "start_at": "not-a-date"}, "…"
    )
    results = assistant.execute_actions(memory, [action], timezone="UTC")
    assert results[0]["ok"] is False
    assert "couldn't apply" in results[0]["detail"]


def test_execute_outing_op_on_stale_id_reports_softly(memory):
    """An outing_id that never existed fails per-action, not with a raise."""
    action = assistant.ValidatedAction(
        "set_outing_window", {"outing_id": 12345, "time_window_minutes": 20.0}, "…"
    )
    results = assistant.execute_actions(memory, [action], timezone="UTC")
    assert results[0]["ok"] is False
    assert results[0]["detail"]


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


def test_execute_isolates_non_value_error(memory):
    """A store call raising something *other* than ValueError is still reported
    per-action rather than propagating — which would abort the batch and 500 the
    endpoint, dropping every later valid action."""

    class Boom:
        user_id = memory.user_id

        def set_todo_priority(self, *a, **k):
            raise RuntimeError("db is on fire")

        def close_todo(self, *a, **k):
            return True

    actions = [
        assistant.ValidatedAction("set_priority", {"todo_id": 1, "priority": 3}, "…"),
        assistant.ValidatedAction("complete_todo", {"todo_id": 1}, "…"),
    ]
    results = assistant.execute_actions(Boom(), actions, timezone="UTC")
    assert results[0]["ok"] is False and "couldn't apply" in results[0]["detail"]
    assert results[1]["ok"] is True  # the batch continued past the failing action


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


def test_allowed_ops_is_derived_from_the_validator_registry():
    """The whitelist and the dispatch table are one source of truth (§11)."""
    from prefrontal.assistant import _VALIDATORS, ALLOWED_OPS

    assert set(_VALIDATORS) == ALLOWED_OPS
    # Every op resolves to a callable validator.
    assert all(callable(v) for v in _VALIDATORS.values())


def test_validate_one_rejects_an_unregistered_op():
    """An op with no validator is refused before execution (the boundary holds)."""
    from prefrontal.assistant import _ActionError, _validate_one

    with pytest.raises(_ActionError):
        _validate_one({"op": "rm_rf_everything"}, {})
