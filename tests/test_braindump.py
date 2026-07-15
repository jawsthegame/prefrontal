"""Tests for the voice brain-dump fan-out (roadmap M1, "capture at speed of thought").

A brain-dump is one rambling utterance fanned out to *both* existing capture
paths — the NL editing assistant (actionable items → a previewable action list)
and the LLM sensor (behavioral asides → pending candidates). These tests never
touch a real model: fake clients return canned JSON so behavior is deterministic.

The invariants under test are the two safety models, carried through the merge:
actionable edits are previewed and written only on Apply; behavioral candidates
land *pending* and apply only on accept — a rambling dump can never silently
mutate the store.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prefrontal.braindump import OnDeviceParse, plan_braindump
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

SECRET = "braindump-secret"

# A ramble that carries both an actionable edit and a behavioral aside.
_ASSISTANT_REPLY = json.dumps(
    {
        "reply": "I'll add a todo to call the dentist.",
        "actions": [{"op": "add_todo", "title": "Call the dentist", "priority": 2}],
    }
)
_SENSOR_REPLY = json.dumps(
    [
        {
            "kind": "state",
            "key": "preferred_briefing_format",
            "value": "short",
            "rationale": "keep the briefings short",
        }
    ]
)


class _FakeClient:
    """A model stand-in returning a fixed reply (or raising)."""

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


class _RoutingClient(_FakeClient):
    """Returns the sensor's array or the assistant's object by the system prompt.

    The endpoint drives both halves through provider-selected clients; a single
    instance handed to both must answer each in its own shape. The sensor's system
    prompt is the "note-reader" one; everything else is the editing assistant.
    """

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if system and "note-reader" in system:
            return _SENSOR_REPLY
        return _ASSISTANT_REPLY


# --- module: plan_braindump ------------------------------------------------


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def test_plan_fans_out_to_both_paths(store):
    """One ramble yields both a validated action and a behavioral candidate."""
    plan = plan_braindump(
        "call the dentist, and keep my briefings short",
        store,
        assistant_client=_FakeClient(_ASSISTANT_REPLY),
        sensor_client=_FakeClient(_SENSOR_REPLY),
    )
    assert plan.reply == "I'll add a todo to call the dentist."
    assert [a.op for a in plan.actions] == ["add_todo"]
    assert [c.payload["key"] for c in plan.candidates] == ["preferred_briefing_format"]
    # Planning writes nothing on either half.
    assert store.open_todos() == []
    assert store.list_proposals("pending") == []


def test_plan_without_sensor_client_skips_behavioral_half(store):
    """No sensor client ⇒ actions still planned, but no candidates guessed."""
    plan = plan_braindump(
        "call the dentist",
        store,
        assistant_client=_FakeClient(_ASSISTANT_REPLY),
        sensor_client=None,
    )
    assert [a.op for a in plan.actions] == ["add_todo"]
    assert plan.candidates == []


def test_plan_degrades_when_models_unreachable(store):
    """Both halves down ⇒ a graceful empty plan, not an exception."""
    plan = plan_braindump(
        "whatever",
        store,
        assistant_client=_FakeClient(error=True),
        sensor_client=_FakeClient(error=True),
    )
    assert plan.actions == []
    assert plan.candidates == []


def test_plan_empty_text_short_circuits_without_model_call(store):
    # A client that fails if touched — blank input must not call either model.
    plan = plan_braindump(
        "   ",
        store,
        assistant_client=_FakeClient(error=True),
        sensor_client=_FakeClient(error=True),
    )
    assert plan.reply == ""
    assert plan.actions == [] and plan.candidates == []


# --- module: on-device parse (no server model) -----------------------------


def _boom_client() -> _FakeClient:
    """A client that raises if any method is touched — proves no model was called."""
    return _FakeClient(error=True)


def test_on_device_parse_skips_both_models(store):
    """A supplied parse validates through the same gates with no model call."""
    plan = plan_braindump(
        "",  # text ignored on the on-device path
        store,
        # Hand both halves a client that would raise if touched.
        assistant_client=_boom_client(),
        sensor_client=_boom_client(),
        parse=OnDeviceParse(
            reply="I'll add a todo to call the dentist.",
            actions=[{"op": "add_todo", "title": "Call the dentist", "priority": 2}],
            observations=[
                {
                    "kind": "state",
                    "key": "preferred_briefing_format",
                    "value": "short",
                    "rationale": "keep the briefings short",
                }
            ],
        ),
    )
    assert plan.reply == "I'll add a todo to call the dentist."
    assert [a.op for a in plan.actions] == ["add_todo"]
    assert [c.payload["key"] for c in plan.candidates] == ["preferred_briefing_format"]
    # Same safety model as the server path: planning writes nothing.
    assert store.open_todos() == []
    assert store.list_proposals("pending") == []


def test_on_device_parse_revalidates_actions_against_store(store):
    """An on-device action is untrusted — a bad id drops in validation, like a model's."""
    plan = plan_braindump(
        "",
        store,
        parse=OnDeviceParse(
            actions=[
                {"op": "add_todo", "title": "Real todo"},
                # References a todo that doesn't exist — must be dropped, not applied.
                {"op": "complete_todo", "todo_id": 9999},
            ],
        ),
    )
    assert [a.op for a in plan.actions] == ["add_todo"]
    assert plan.errors  # the bogus completion was reported, not silently accepted


def test_on_device_parse_drops_off_allowlist_observations(store):
    """An on-device observation can't slip an off-allowlist candidate past the gate."""
    plan = plan_braindump(
        "",
        store,
        parse=OnDeviceParse(
            observations=[
                {"kind": "state", "key": "preferred_briefing_format", "value": "short"},
                {"kind": "state", "key": "not_a_real_key", "value": "x"},
                {"kind": "episode", "episode_type": "not_a_type"},
            ],
        ),
    )
    assert [c.payload["key"] for c in plan.candidates] == ["preferred_briefing_format"]


def test_on_device_parse_empty_yields_empty_plan(store):
    """An empty parse is a valid no-op, not a crash — no reply, no items.

    A true no-op stays silent (reply=""), like the empty-text short-circuit —
    not a misleading "I didn't find anything to change."
    """
    plan = plan_braindump("", store, parse=OnDeviceParse())
    assert plan.reply == ""
    assert plan.actions == [] and plan.candidates == []


# --- endpoint: POST /braindump ---------------------------------------------


@pytest.fixture()
def app_store():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="Tester", token=SECRET, is_operator=True)
    try:
        yield unscoped
    finally:
        conn.close()


@pytest.fixture()
def user_store(app_store):
    return app_store.scoped(app_store.get_user("tester")["id"])


def _client(app_store, client):
    # ``sensor`` isn't an Anthropic agent by default, so it resolves to the local
    # (ollama) client — hand the same routing fake to both so each half answers.
    app = create_app(store=app_store, settings=Settings(), ollama=client, anthropic=client)
    return TestClient(app)


def test_braindump_requires_auth(app_store):
    with _client(app_store, _RoutingClient()) as c:
        assert c.post("/braindump", json={"text": "hi"}).status_code == 401


def test_braindump_blank_text_422(app_store):
    with _client(app_store, _RoutingClient()) as c:
        resp = c.post(
            "/braindump", json={"text": "   "}, headers={"X-Prefrontal-Token": SECRET}
        )
    assert resp.status_code == 422


def test_braindump_previews_actions_and_records_proposals(app_store, user_store):
    """Actions are a preview (unwritten); candidates land pending in one call."""
    with _client(app_store, _RoutingClient()) as c:
        resp = c.post(
            "/braindump",
            json={"text": "call the dentist, and keep my briefings short"},
            headers={"X-Prefrontal-Token": SECRET},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["actions"]) == 1
        assert body["actions"][0]["op"] == "add_todo"
        assert len(body["proposals"]) == 1
        assert body["proposals"][0]["status"] == "pending"
        # The two halves report their provider independently (they can diverge).
        assert set(body["provider"]) == {"assistant", "sensor"}

        # The actionable half wrote nothing yet…
        assert user_store.open_todos() == []
        # …but the behavioral candidate is a pending proposal, in the review queue.
        pending = c.get("/proposals", headers={"X-Prefrontal-Token": SECRET}).json()
        assert len(pending["proposals"]) == 1

        # Applying the echoed actions writes the todo (existing confirm path).
        applied = c.post(
            "/assistant/apply",
            json={"actions": body["actions"]},
            headers={"X-Prefrontal-Token": SECRET},
        ).json()
    assert applied["applied"] == 1
    todos = user_store.open_todos()
    assert len(todos) == 1 and todos[0]["title"] == "Call the dentist"


def test_braindump_actions_only_when_sensor_finds_nothing(app_store, user_store):
    """A dump with no behavioral signal still returns its actionable edits."""

    class _NoSensor(_RoutingClient):
        def generate(self, prompt: str, *, system: str | None = None) -> str:
            if system and "note-reader" in system:
                return "[]"
            return _ASSISTANT_REPLY

    with _client(app_store, _NoSensor()) as c:
        body = c.post(
            "/braindump",
            json={"text": "call the dentist"},
            headers={"X-Prefrontal-Token": SECRET},
        ).json()
    assert len(body["actions"]) == 1
    assert body["proposals"] == []


def test_braindump_on_device_parse_calls_no_model(app_store, user_store):
    """A ``parse`` body validates + records without touching the server model."""

    class _Boom(_RoutingClient):
        def generate(self, prompt: str, *, system: str | None = None) -> str:
            raise AssertionError("on-device parse must not call the server model")

    with _client(app_store, _Boom()) as c:
        resp = c.post(
            "/braindump",
            json={
                "parse": {
                    "reply": "I'll add a todo to call the dentist.",
                    "actions": [
                        {"op": "add_todo", "title": "Call the dentist", "priority": 2}
                    ],
                    "observations": [
                        {
                            "kind": "state",
                            "key": "preferred_briefing_format",
                            "value": "short",
                        }
                    ],
                }
            },
            headers={"X-Prefrontal-Token": SECRET},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "I'll add a todo to call the dentist."
        assert [a["op"] for a in body["actions"]] == ["add_todo"]
        assert len(body["proposals"]) == 1
        assert body["proposals"][0]["status"] == "pending"
        # Both halves report the on-device provider so telemetry sees the path.
        assert body["provider"] == {"assistant": "on_device", "sensor": "on_device"}

        # Same confirm gates: the action is unwritten, the candidate lands pending.
        assert user_store.open_todos() == []
        pending = c.get("/proposals", headers={"X-Prefrontal-Token": SECRET}).json()
        assert len(pending["proposals"]) == 1

        # Applying the echoed on-device actions writes the todo (existing path).
        applied = c.post(
            "/assistant/apply",
            json={"actions": body["actions"]},
            headers={"X-Prefrontal-Token": SECRET},
        ).json()
    assert applied["applied"] == 1
    todos = user_store.open_todos()
    assert len(todos) == 1 and todos[0]["title"] == "Call the dentist"


def test_braindump_empty_body_422(app_store):
    """Neither text nor a parse ⇒ 422 (nothing to capture)."""
    with _client(app_store, _RoutingClient()) as c:
        resp = c.post(
            "/braindump", json={}, headers={"X-Prefrontal-Token": SECRET}
        )
    assert resp.status_code == 422
