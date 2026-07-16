"""Tests for scoped agentic execution (roadmap M4): MCP tool-calls behind a gate.

Two layers: the pure core (`preview_action` / `run_action` / `list_available_tools`
in `prefrontal/actions.py`) against a fake MCP server, and the HTTP surface
(`GET /actions/tools`, `POST /actions/preview`, `POST /actions/run`). The safety
properties under test: the allowlist gate, the digest-pinned preview→confirm, and
the audit trace.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.actions import (
    ACTION_EPISODE,
    list_available_tools,
    preview_action,
    run_action,
)
from prefrontal.config import McpServerConfig, Settings
from prefrontal.integrations.mcp import McpClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "actions-secret"


def _server_transport(tools, *, call_text="done", call_is_error=False):
    """A MockTransport JSON-RPC MCP server advertising `tools`."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        rid, method = body["id"], body.get("method")
        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})

        if method == "initialize":
            return ok({"capabilities": {}})
        if method == "tools/list":
            return ok({"tools": tools})
        if method == "tools/call":
            return ok({"content": [{"type": "text", "text": call_text}], "isError": call_is_error})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": {"message": "x"}})

    return httpx.MockTransport(handler)


def _factory(tools, **kw):
    """A client_factory that returns an McpClient wired to a fake server."""
    return lambda server: McpClient(server.url, transport=_server_transport(tools, **kw))


def _settings(*, allowed=("create_event",)):
    return Settings(
        webhook_secret=SECRET,
        mcp_servers=(
            McpServerConfig(name="calendar", url="https://mcp.test/rpc",
                            allowed_tools=frozenset(allowed)),
        ),
    )


CAL_TOOLS = [
    {"name": "create_event", "description": "Book a calendar event", "inputSchema": {}},
    {"name": "delete_event", "description": "Delete an event", "inputSchema": {}},
]


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


# --- listing (allowlist ∩ advertised) ----------------------------------------


def test_list_available_tools_intersects_allowlist_and_advertised():
    # Server offers create_event + delete_event; only create_event is allowlisted.
    tools = list_available_tools(_settings(), client_factory=_factory(CAL_TOOLS))
    assert [t["tool"] for t in tools] == ["create_event"]
    assert tools[0]["server"] == "calendar"


def test_list_available_tools_empty_when_no_servers():
    assert list_available_tools(Settings(), client_factory=_factory(CAL_TOOLS)) == []


# --- preview -----------------------------------------------------------------


def test_preview_happy_path():
    p = preview_action(_settings(), "calendar", "create_event", {"title": "Dentist"},
                       client_factory=_factory(CAL_TOOLS))
    assert p.can_run is True and p.blockers == []
    assert p.description == "Book a calendar event"
    assert p.digest


def test_preview_blocks_unknown_server():
    p = preview_action(_settings(), "nope", "create_event", client_factory=_factory(CAL_TOOLS))
    assert p.can_run is False
    assert any("no MCP server" in b for b in p.blockers)


def test_preview_blocks_tool_not_allowlisted():
    # delete_event is advertised but not on the allowlist.
    p = preview_action(_settings(), "calendar", "delete_event", client_factory=_factory(CAL_TOOLS))
    assert p.can_run is False
    assert any("not on" in b and "allowlist" in b for b in p.blockers)


def test_preview_blocks_tool_not_advertised():
    # Allowlisted, but the server offers no such tool right now.
    p = preview_action(_settings(allowed=("create_event",)), "calendar", "create_event",
                       client_factory=_factory([]))
    assert p.can_run is False
    assert any("doesn't currently offer" in b for b in p.blockers)


# --- run ---------------------------------------------------------------------


def test_run_happy_path_calls_tool_and_audits(store):
    p = preview_action(_settings(), "calendar", "create_event", {"title": "Dentist"},
                       client_factory=_factory(CAL_TOOLS))
    outcome = run_action(store, _settings(), "calendar", "create_event", {"title": "Dentist"},
                         expected_digest=p.digest,
                         client_factory=_factory(CAL_TOOLS, call_text="booked"))
    assert outcome.ran is True and outcome.code == "ran"
    assert outcome.content == "booked"
    # an inert audit episode was logged
    audit = store.episodes_by_type(ACTION_EPISODE, limit=10)
    assert audit and audit[0]["context"] == "calendar.create_event"


def test_run_refuses_stale_digest(store):
    outcome = run_action(store, _settings(), "calendar", "create_event", {"title": "X"},
                         expected_digest="wrong", client_factory=_factory(CAL_TOOLS))
    assert outcome.ran is False and outcome.code == "stale"
    assert store.episodes_by_type(ACTION_EPISODE, limit=10) == []  # nothing ran, nothing audited


def test_run_refuses_empty_digest(store):
    outcome = run_action(store, _settings(), "calendar", "create_event", {},
                         expected_digest="", client_factory=_factory(CAL_TOOLS))
    assert outcome.ran is False and outcome.code == "stale"


def test_run_blocks_tool_not_allowlisted(store):
    outcome = run_action(store, _settings(), "calendar", "delete_event", {},
                         expected_digest=None, client_factory=_factory(CAL_TOOLS))
    assert outcome.ran is False and outcome.code == "blocked"


def test_run_tool_error_is_failed_not_raised(store):
    p = preview_action(_settings(), "calendar", "create_event", {},
                       client_factory=_factory(CAL_TOOLS))
    outcome = run_action(store, _settings(), "calendar", "create_event", {},
                         expected_digest=p.digest,
                         client_factory=_factory(CAL_TOOLS, call_is_error=True))
    assert outcome.ran is False and outcome.code == "failed"


# --- HTTP surface ------------------------------------------------------------


def _headers():
    return {"X-Prefrontal-Token": SECRET}


@pytest.fixture()
def http():
    from prefrontal.webhooks.app import create_app

    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    app = create_app(store=unscoped, settings=_settings())
    app.state.mcp_client_factory = _factory(CAL_TOOLS, call_text="booked")
    with TestClient(app) as c:
        yield c
    conn.close()


def test_http_requires_auth(http):
    assert http.get("/actions/tools").status_code == 401
    r = http.post("/actions/preview", json={"server": "calendar", "tool": "create_event"})
    assert r.status_code == 401


def test_http_list_tools(http):
    tools = http.get("/actions/tools", headers=_headers()).json()["tools"]
    assert [t["tool"] for t in tools] == ["create_event"]


def test_http_preview_then_run_flow(http):
    preview = http.post(
        "/actions/preview",
        json={"server": "calendar", "tool": "create_event", "arguments": {"title": "Dentist"}},
        headers=_headers(),
    ).json()
    assert preview["can_run"] is True
    r = http.post(
        "/actions/run",
        json={"server": "calendar", "tool": "create_event", "arguments": {"title": "Dentist"},
              "preview_digest": preview["digest"]},
        headers=_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ran"] is True and body["content"] == "booked"


def test_http_run_stale_digest_is_409(http):
    r = http.post(
        "/actions/run",
        json={"server": "calendar", "tool": "create_event", "arguments": {},
              "preview_digest": "stale"},
        headers=_headers(),
    )
    assert r.status_code == 409


def test_http_run_blocked_is_422(http):
    # delete_event isn't allowlisted → a structural blocker.
    preview = http.post(
        "/actions/preview",
        json={"server": "calendar", "tool": "delete_event"},
        headers=_headers(),
    ).json()
    assert preview["can_run"] is False
    r = http.post(
        "/actions/run",
        json={"server": "calendar", "tool": "delete_event", "preview_digest": "anything"},
        headers=_headers(),
    )
    assert r.status_code == 422


def test_http_run_empty_digest_rejected(http):
    r = http.post(
        "/actions/run",
        json={"server": "calendar", "tool": "create_event", "preview_digest": ""},
        headers=_headers(),
    )
    assert r.status_code == 422  # schema min_length
