"""Tests for the minimal MCP client (roadmap M4 transport).

Drives `McpClient` against a fake JSON-RPC server (httpx MockTransport) covering
the initialize→tools/list→tools/call flow, the isError and transport-failure
paths, and the SSE response variant. No network.
"""

from __future__ import annotations

import json

import httpx

from prefrontal.integrations.mcp import McpClient, _flatten_content


def _server(tools=None, *, call_text="done", call_is_error=False, http_status=200):
    """A MockTransport JSON-RPC MCP server; notifications get a bare 202."""
    tools = tools if tools is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:  # a notification (notifications/initialized)
            return httpx.Response(202)
        if http_status != 200:
            return httpx.Response(http_status)
        rid, method = body["id"], body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18",
                      "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}},
                headers={"Mcp-Session-Id": "sess-1"},
            )
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
            )
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": call_text}], "isError": call_is_error}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": -32601, "message": "no such method"}})

    return httpx.MockTransport(handler)


def _client(**kw):
    return McpClient("https://mcp.test/rpc", transport=_server(**kw))


def test_list_tools_parses_advertised_tools():
    tools = [
        {"name": "create_event", "description": "Book it", "inputSchema": {"type": "object"}},
        {"name": "noname_dropped"},  # kept (has a name)
        {"nope": 1},  # dropped (no name)
    ]
    got = _client(tools=tools).list_tools()
    names = [t.name for t in got]
    assert "create_event" in names and "noname_dropped" in names
    assert len(got) == 2
    ce = next(t for t in got if t.name == "create_event")
    assert ce.description == "Book it" and ce.input_schema == {"type": "object"}


def test_list_tools_empty_on_transport_error():
    assert _client(http_status=500).list_tools() == []


def test_call_tool_success_returns_content():
    r = _client(call_text="event created").call_tool("create_event", {"title": "Dentist"})
    assert r.ok is True
    assert r.content == "event created"


def test_call_tool_is_error_marks_not_ok():
    r = _client(call_is_error=True).call_tool("create_event", {})
    assert r.ok is False
    assert "reported an error" in r.detail


def test_call_tool_transport_error_is_not_ok_and_never_raises():
    r = _client(http_status=500).call_tool("create_event", {})
    assert r.ok is False and r.content == ""


def test_flatten_content_joins_text_blocks():
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _flatten_content(blocks) == "a\nb"
    assert _flatten_content("not a list") == ""


def test_decode_handles_sse_response():
    """A text/event-stream reply is decoded from its first data: line."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        rid = body["id"]
        if body["method"] == "initialize":
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}}
        else:
            payload = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "t"}]}}
        return httpx.Response(
            200, text=f"event: message\ndata: {json.dumps(payload)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    client = McpClient("https://mcp.test/rpc", transport=httpx.MockTransport(handler))
    assert [t.name for t in client.list_tools()] == ["t"]
