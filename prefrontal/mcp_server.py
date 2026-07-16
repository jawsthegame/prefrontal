"""A built-in MCP server exposing Prefrontal's own bounded actions as tools.

Roadmap M4's other half: having built the *client* that calls external MCP tools
(`prefrontal/actions.py`), this lets Prefrontal itself *serve* a small, scoped set
of its own capabilities as MCP tools — so the delegation agent, an external agent,
or Prefrontal's own action gate can "create the calendar event", "place a scoped
reminder call", or "capture a todo" through the standard protocol, without the user
standing up any third-party server. Local-first: it's just another authenticated
endpoint on the existing app.

Each tool is a thin, typed wrapper over an operation the app already exposes — no
new capability, just an MCP surface for it — so the safety model is the app's
existing one:

- **Auth + scoping** is the caller's per-user token (handled at the HTTP layer):
  every tool acts on *that user's* own data through the scoped store.
- **Operator gate per tool.** A tool that spends an *operator*-configured resource
  (``place_call`` uses the operator's Twilio account) is marked ``operator_only``
  and refused for non-operators — the same rule the ``/actions`` client surface
  follows.
- **No new autonomy.** ``create_event`` is no more powerful than ``POST
  /commitments``; ``place_call`` only ever dials the caller's *own* configured
  number (``resolve_route``'s ``twilio_to``), never an arbitrary recipient — a
  scoped "call me and say X" reminder, not an outbound dialer.

The protocol handling is a tiny JSON-RPC dispatcher (``initialize`` / ``tools/list``
/ ``tools/call``); a tool-level failure comes back as an MCP ``isError`` result,
not a protocol error, per the MCP convention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prefrontal.commitments import normalize_event
from prefrontal.config import Settings

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: The MCP protocol revision this server advertises.
PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    """A tool-level failure (bad args, precondition) — surfaced as an MCP isError."""


@dataclass(frozen=True)
class ToolContext:
    """What a tool handler gets: the caller's scoped store, settings, and role.

    ``voice_client`` is an optional injected transport for :func:`_place_call`
    (tests pass a fake); unset, the handler builds a real client.
    """

    store: MemoryStore
    settings: Settings
    is_operator: bool = False
    voice_client: Any = None


ToolHandler = Callable[["ToolContext", dict[str, Any]], str]


@dataclass(frozen=True)
class Tool:
    """A served MCP tool: its schema and the handler that runs it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    operator_only: bool = False

    def spec(self) -> dict[str, Any]:
        """The ``tools/list`` entry for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# --- tool handlers -----------------------------------------------------------


def _create_event(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Create a calendar commitment on the caller's schedule."""
    title = str(args.get("title", "")).strip()
    start = str(args.get("start", "")).strip()
    if not title:
        raise ToolError("'title' is required")
    if not start:
        raise ToolError("'start' is required (an ISO 8601 timestamp)")
    event: dict[str, Any] = {"title": title, "start_at": start, "source": "manual"}
    for src, dst in (("end", "end_at"), ("notes", "notes"), ("location", "location")):
        val = str(args.get(src, "")).strip()
        if val:
            event[dst] = val
    try:
        fields = normalize_event(event, default_tz=ctx.settings.timezone)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    commitment_id, _ = ctx.store.upsert_commitment(**fields)
    return f"Created event '{title}' (commitment {commitment_id})."


def _create_todo(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Capture a todo on the caller's list."""
    title = str(args.get("title", "")).strip()
    if not title:
        raise ToolError("'title' is required")
    notes = str(args.get("notes", "")).strip() or None
    deadline = str(args.get("deadline", "")).strip() or None
    todo_id = ctx.store.add_todo(title, notes=notes, deadline=deadline, source="mcp")
    return f"Added todo '{title}' (id {todo_id})."


def _place_call(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Place a scoped reminder call to the caller's *own* configured number."""
    message = str(args.get("message", "")).strip()
    if not message:
        raise ToolError("'message' is required (what the call should say)")
    # Lazy import: delivery reaches up into webhooks/coaching, so keep it off the
    # module import path (matching how todos.py reaches the delivery client).
    from prefrontal.integrations.delivery import TwilioVoiceClient, resolve_route

    route = resolve_route(ctx.store, ctx.settings)
    if not (route.twilio_account_sid and route.twilio_auth_token
            and route.twilio_from and route.twilio_to):
        raise ToolError("no phone-call route is configured for you (Twilio account + your number)")
    client = ctx.voice_client or TwilioVoiceClient()
    result = client.call(
        route.twilio_account_sid,
        route.twilio_auth_token,
        sender=route.twilio_from,
        to=route.twilio_to,
        message=message,
    )
    if not result.delivered:
        raise ToolError(f"the call wasn't placed: {result.detail}")
    return f"Placed a reminder call to your number ({result.detail})."


#: The served tool registry. Add an entry to expose a new bounded capability.
TOOLS: dict[str, Tool] = {
    "create_event": Tool(
        name="create_event",
        description="Create a calendar event/commitment on your schedule.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What the event is."},
                "start": {"type": "string", "description": "Start time (ISO 8601)."},
                "end": {"type": "string", "description": "Optional end time (ISO 8601)."},
                "notes": {"type": "string", "description": "Optional detail."},
                "location": {"type": "string", "description": "Optional location."},
            },
            "required": ["title", "start"],
        },
        handler=_create_event,
    ),
    "create_todo": Tool(
        name="create_todo",
        description="Add a todo to your list.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What needs doing."},
                "notes": {"type": "string", "description": "Optional detail."},
                "deadline": {
                    "type": "string",
                    "description": "Optional UTC deadline, 'YYYY-MM-DD HH:MM:SS'.",
                },
            },
            "required": ["title"],
        },
        handler=_create_todo,
    ),
    "place_call": Tool(
        name="place_call",
        description="Call your own phone and speak a short message (a scoped reminder call).",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What the call should say."},
            },
            "required": ["message"],
        },
        handler=_place_call,
        operator_only=True,
    ),
}


# --- JSON-RPC dispatch -------------------------------------------------------


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(rid: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def handle_rpc(ctx: ToolContext, body: Any) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns the response, or ``None`` for a notification.

    Handles ``initialize`` (handshake), ``tools/list`` (the registry), and
    ``tools/call`` (run a tool → an ``isError`` result on a tool-level failure).
    An unknown method is a JSON-RPC ``-32601``; a notification (no ``id``) gets no
    response.
    """
    if not isinstance(body, dict):
        return _error(None, -32700, "parse error: expected a JSON-RPC object")
    method = body.get("method")
    rid = body.get("id")
    if rid is None:  # a notification (e.g. notifications/initialized) — no reply
        return None
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "prefrontal", "version": "0.1"},
        })
    if method == "tools/list":
        return _ok(rid, {"tools": [t.spec() for t in TOOLS.values()]})
    if method == "tools/call":
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        name = str(params.get("name", ""))
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        tool = TOOLS.get(name)
        if tool is None:
            return _tool_result(rid, f"unknown tool '{name}'", is_error=True)
        if tool.operator_only and not ctx.is_operator:
            return _tool_result(rid, f"'{name}' requires operator privileges", is_error=True)
        try:
            text = tool.handler(ctx, arguments)
        except ToolError as exc:
            return _tool_result(rid, str(exc), is_error=True)
        return _tool_result(rid, text)
    return _error(rid, -32601, f"unknown method '{method}'")
