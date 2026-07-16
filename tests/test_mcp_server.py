"""Tests for Prefrontal's built-in MCP server (roadmap M4).

Two layers: the pure JSON-RPC dispatch + tool handlers (`handle_rpc` in
`prefrontal/mcp_server.py`) against an in-memory scoped store, and the HTTP
endpoint (`POST /mcp`). Covers the handshake, tools/list, each tool's success and
error paths, the per-tool operator gate, and notification handling.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.mcp_server import PROTOCOL_VERSION, ToolContext, handle_rpc
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "mcp-server-secret"
MEMBER_SECRET = "mcp-server-member-secret"


class _FakeVoice:
    """A stand-in for TwilioVoiceClient recording placed calls."""

    def __init__(self, *, delivered=True):
        self.delivered = delivered
        self.calls: list[dict] = []

    def call(self, sid, auth, *, sender, to, message):
        self.calls.append({"sid": sid, "sender": sender, "to": to, "message": message})
        return SimpleNamespace(delivered=self.delivered, detail="twilio call responded 201")


def _rpc(method, *, rid=1, params=None):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return body


def _call(name, arguments=None, *, rid=1):
    return _rpc("tools/call", rid=rid, params={"name": name, "arguments": arguments or {}})


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _ctx(store, *, is_operator=True, voice=None, settings=None):
    return ToolContext(
        store=store,
        settings=settings or Settings(),
        is_operator=is_operator,
        voice_client=voice,
    )


# --- protocol ----------------------------------------------------------------


def test_initialize_returns_handshake(store):
    r = handle_rpc(_ctx(store), _rpc("initialize"))
    assert r["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"] == "prefrontal"


def test_tools_list_advertises_the_registry(store):
    r = handle_rpc(_ctx(store), _rpc("tools/list"))
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"create_event", "create_todo", "place_call"}
    # each carries an input schema
    assert all("inputSchema" in t for t in r["result"]["tools"])


def test_notification_gets_no_response(store):
    assert handle_rpc(_ctx(store), _rpc("notifications/initialized", rid=None)) is None


def test_unknown_method_is_jsonrpc_error(store):
    r = handle_rpc(_ctx(store), _rpc("frobnicate"))
    assert r["error"]["code"] == -32601


def test_non_object_body_is_parse_error(store):
    r = handle_rpc(_ctx(store), ["not", "an", "object"])
    assert r["error"]["code"] == -32700


# --- create_event ------------------------------------------------------------


def test_create_event_creates_a_commitment(store):
    r = handle_rpc(_ctx(store), _call("create_event", {
        "title": "Dentist", "start": "2026-08-01T09:00:00Z", "notes": "cleaning"}))
    assert r["result"]["isError"] is False
    text = r["result"]["content"][0]["text"]
    assert "Created event 'Dentist'" in text
    cid = int(re.search(r"commitment (\d+)", text).group(1))
    assert store.get_commitment(cid)["title"] == "Dentist"


def test_create_event_missing_start_is_tool_error(store):
    r = handle_rpc(_ctx(store), _call("create_event", {"title": "Dentist"}))
    assert r["result"]["isError"] is True
    assert "start" in r["result"]["content"][0]["text"]


# --- create_todo -------------------------------------------------------------


def test_create_todo_adds_a_todo(store):
    r = handle_rpc(_ctx(store), _call("create_todo", {"title": "Call the plumber"}))
    assert r["result"]["isError"] is False
    assert any(t["title"] == "Call the plumber" for t in store.open_todos())


def test_create_todo_missing_title_is_tool_error(store):
    r = handle_rpc(_ctx(store), _call("create_todo", {}))
    assert r["result"]["isError"] is True


# --- place_call (operator-gated + scoped to the caller's own number) ---------


def _twilio_settings():
    return Settings(twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from="+15550000000")


def test_place_call_requires_operator(store):
    r = handle_rpc(_ctx(store, is_operator=False), _call("place_call", {"message": "leave now"}))
    assert r["result"]["isError"] is True
    assert "operator" in r["result"]["content"][0]["text"]


def test_place_call_without_route_is_tool_error(store):
    # Operator, but no Twilio route configured.
    r = handle_rpc(_ctx(store, voice=_FakeVoice()), _call("place_call", {"message": "leave now"}))
    assert r["result"]["isError"] is True
    assert "no phone-call route" in r["result"]["content"][0]["text"]


def test_place_call_dials_the_users_own_number(store):
    store.set_state("twilio_to", "+15551234567")  # the caller's own number
    voice = _FakeVoice()
    r = handle_rpc(
        _ctx(store, voice=voice, settings=_twilio_settings()),
        _call("place_call", {"message": "time to leave"}),
    )
    assert r["result"]["isError"] is False
    assert voice.calls == [{
        "sid": "AC1", "sender": "+15550000000", "to": "+15551234567", "message": "time to leave"}]


def test_place_call_missing_message_is_tool_error(store):
    r = handle_rpc(_ctx(store, settings=_twilio_settings()), _call("place_call", {}))
    assert r["result"]["isError"] is True


def test_unknown_tool_is_tool_error(store):
    r = handle_rpc(_ctx(store), _call("delete_everything", {}))
    assert r["result"]["isError"] is True
    assert "unknown tool" in r["result"]["content"][0]["text"]


# --- HTTP surface ------------------------------------------------------------


def _headers(token=SECRET):
    return {"X-Prefrontal-Token": token}


@pytest.fixture()
def http():
    from prefrontal.webhooks.app import create_app

    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    provision_user(unscoped, "kid", display_name="Kid", token=MEMBER_SECRET, is_operator=False)
    app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c
    conn.close()


def test_http_requires_auth(http):
    assert http.post("/mcp", json=_rpc("tools/list")).status_code == 401


def test_http_tools_call_creates_a_todo(http):
    r = http.post("/mcp", json=_call("create_todo", {"title": "Buy milk"}), headers=_headers())
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is False


def test_http_notification_returns_202(http):
    r = http.post("/mcp", json=_rpc("notifications/initialized", rid=None), headers=_headers())
    assert r.status_code == 202


def test_http_place_call_operator_gate(http):
    # A non-operator household member can't place a call (operator-configured Twilio).
    r = http.post(
        "/mcp", json=_call("place_call", {"message": "hi"}), headers=_headers(MEMBER_SECRET)
    )
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True
    assert "operator" in r.json()["result"]["content"][0]["text"]
