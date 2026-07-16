"""Minimal client for a Model Context Protocol (MCP) server.

MCP is roadmap M4's anchor: *scoped agentic execution via structured tool-calls*.
An MCP server exposes a set of **tools** (typed functions — "create a calendar
event", "file a form") that a client can list and call over JSON-RPC. This client
wraps just the two calls the confirmed-action provider needs — ``tools/list`` and
``tools/call`` — over MCP's **Streamable HTTP** transport, deliberately tiny and
synchronous like :class:`~prefrontal.integrations.ollama.OllamaClient` (these run
on a user's confirmation, not a hot path).

It is *only* a transport. All of the safety — which servers exist, which tools are
callable (the allowlist), and the preview→confirm gate before anything fires —
lives one layer up in :mod:`prefrontal.actions`. This module never decides *whether*
to call a tool, only *how* to. Errors surface as :class:`McpError` (a
:class:`~prefrontal.integrations.base.ProviderError`) so the action layer can treat
an unreachable/again-failing server the way it treats any provider outage.

Scope note (v1): HTTP transport only (stdio-subprocess servers are a follow-up),
and a fresh ``initialize`` handshake per operation rather than a reused session —
simple and correct; session reuse is an optimization for later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from prefrontal.integrations.base import ProviderError

#: The MCP protocol revision this client advertises in ``initialize``.
_PROTOCOL_VERSION = "2025-06-18"


class McpError(ProviderError):
    """Raised when an MCP request fails (transport, non-2xx, or a JSON-RPC error)."""


@dataclass(frozen=True)
class McpTool:
    """A tool advertised by a server: its name, description, and input schema."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpResult:
    """The outcome of a ``tools/call``.

    ``ok`` is False when the transport failed *or* the tool itself reported an
    error (``isError``); ``content`` is the flattened text the tool returned;
    ``detail`` is a short human-readable status.
    """

    ok: bool
    content: str = ""
    detail: str = ""


def _flatten_content(raw: Any) -> str:
    """Join an MCP content array's text parts into one string (best-effort)."""
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for block in raw:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


class McpClient:
    """Synchronous client for one MCP server over Streamable HTTP.

    Args:
        url: The server's JSON-RPC endpoint (a full URL).
        auth_token: Optional bearer token (sent as ``Authorization: Bearer …``).
        timeout: Per-request timeout in seconds.
        transport: Optional ``httpx`` transport, primarily for tests.
    """

    def __init__(
        self,
        url: str,
        *,
        auth_token: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.auth_token = auth_token
        self.timeout = timeout
        self._transport = transport
        self._id = 0

    def _headers(self, session_id: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        """Decode a JSON-RPC reply from a JSON or (minimally) an SSE response body."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # Take the first ``data:`` line's JSON payload — enough for a single
            # request/response exchange (we don't stream partials).
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
            raise McpError("empty SSE response")
        return resp.json()

    def _rpc(
        self, method: str, params: dict[str, Any], session_id: str | None
    ) -> tuple[dict, str | None]:
        """Send one JSON-RPC request; return ``(result, session_id)`` or raise."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                resp = client.post(self.url, json=payload, headers=self._headers(session_id))
                if not resp.is_success:
                    raise McpError(f"{method}: HTTP {resp.status_code}")
                body = self._decode(resp)
        except (httpx.HTTPError, ValueError) as exc:
            raise McpError(f"{method}: {exc}") from exc
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise McpError(f"{method}: {msg}")
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise McpError(f"{method}: malformed result")
        return result, resp.headers.get("Mcp-Session-Id") or session_id

    def _notify(self, method: str, session_id: str | None) -> None:
        """Fire a JSON-RPC notification (no id, no response expected); best-effort."""
        payload = {"jsonrpc": "2.0", "method": method, "params": {}}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                client.post(self.url, json=payload, headers=self._headers(session_id))
        except httpx.HTTPError:
            pass

    def _initialize(self) -> str | None:
        """Run the ``initialize`` handshake; return the session id if the server sets one."""
        _result, session = self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "prefrontal", "version": "0.1"},
            },
            None,
        )
        if session:
            self._notify("notifications/initialized", session)
        return session

    def list_tools(self) -> list[McpTool]:
        """List the server's tools. Returns ``[]`` on any failure (availability-style)."""
        try:
            session = self._initialize()
            result, _ = self._rpc("tools/list", {}, session)
        except McpError:
            return []
        raw = result.get("tools")
        if not isinstance(raw, list):
            return []
        tools: list[McpTool] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name", "")).strip()
            if not name:
                continue
            schema = t.get("inputSchema")
            tools.append(
                McpTool(
                    name=name,
                    description=str(t.get("description", "")).strip(),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult:
        """Call a tool. Never raises — a transport/protocol failure is ``ok=False``."""
        try:
            session = self._initialize()
            result, _ = self._rpc("tools/call", {"name": name, "arguments": arguments}, session)
        except McpError as exc:
            return McpResult(ok=False, content="", detail=str(exc))
        is_error = bool(result.get("isError"))
        content = _flatten_content(result.get("content"))
        return McpResult(
            ok=not is_error,
            content=content,
            detail="tool reported an error" if is_error else "ok",
        )
