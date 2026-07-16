"""Scoped agentic execution — call an allowlisted MCP tool, behind a confirm gate.

Roadmap M4's anchor: *"It does the thing, not just reminds"* via **scoped,
verifiable tool-calls** — never an open-ended browser/computer-use agent. This is
the generic provider that the delegation "send the drafted email" action (its
first, native instance) foreshadowed: the same two-phase preview→confirm→execute
gate, now over an arbitrary MCP tool.

The safety model, in one place:

1. **Allowlist.** A tool can be called only if its server is configured
   (`Settings.mcp_servers`) *and* the tool name is in that server's
   ``allowed_tools``. Anything else is refused before a request is made — the
   "bounded action space" the roadmap insists on.
2. **Two-phase gate.** :func:`preview_action` resolves and validates the call and
   returns a ``digest`` pinning the exact ``{server, tool, arguments}``; nothing
   fires. :func:`run_action` re-validates against the *current* config, refuses if
   the digest no longer matches (the confirmed call drifted from the previewed
   one), and only then calls the tool.
3. **Audit.** A run logs an inert ``action`` episode (no outcome/ack, so it never
   pollutes the learning loop) as a queryable trace of what was executed.

The transport (:class:`~prefrontal.integrations.mcp.McpClient`) is injected via a
``client_factory`` so tests can stand in a fake server; unset, it builds a real
HTTP client per configured server.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prefrontal.config import McpServerConfig, Settings
from prefrontal.integrations.mcp import McpClient, McpTool

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: A factory ``(McpServerConfig) -> McpClient`` — injectable for tests.
ClientFactory = Callable[[McpServerConfig], McpClient]

#: episode_type for the audit trace of a run. Deliberately not one of the learning
#: EPISODE_TYPES and not aggregated anywhere, so it records without polluting.
ACTION_EPISODE = "action"


@dataclass(frozen=True)
class ActionPreview:
    """What running ``{server}.{tool}(arguments)`` would do — no side effects.

    ``can_run`` is the go/no-go; ``blockers`` explains any no. ``digest`` pins the
    exact call so :func:`run_action` can refuse a drifted confirmation.
    """

    server: str
    tool: str
    arguments: dict[str, Any]
    description: str
    can_run: bool
    blockers: list[str] = field(default_factory=list)
    digest: str = ""


@dataclass(frozen=True)
class ActionOutcome:
    """The result of a run.

    ``code``: ``ran`` (done), ``blocked`` (a precondition failed), ``stale`` (the
    call drifted from the preview — re-preview), ``failed`` (the tool/transport
    rejected it).
    """

    ran: bool
    code: str
    server: str
    tool: str
    content: str = ""
    detail: str = ""


def _default_factory(server: McpServerConfig) -> McpClient:
    return McpClient(server.url, auth_token=server.auth_token)


def resolve_server(settings: Settings, name: str) -> McpServerConfig | None:
    """The configured MCP server with ``name``, or ``None``."""
    for server in settings.mcp_servers:
        if server.name == name:
            return server
    return None


def action_digest(server: str, tool: str, arguments: dict[str, Any]) -> str:
    """A stable hash pinning the exact call (server, tool, canonical arguments).

    Change-detection only — auth is the scoped store + the server allowlist; this
    just lets the confirm step refuse when the confirmed call differs from what was
    previewed.
    """
    canon = json.dumps(
        {"server": server, "tool": tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def list_available_tools(
    settings: Settings, *, client_factory: ClientFactory | None = None
) -> list[dict[str, Any]]:
    """List the callable (server-configured *and* allowlisted) tools, across servers.

    Only tools the server actually advertises *and* that are on its allowlist are
    returned — an allowlisted name the server no longer offers is silently dropped,
    and a server tool that isn't allowlisted is never surfaced.
    """
    factory = client_factory or _default_factory
    out: list[dict[str, Any]] = []
    for server in settings.mcp_servers:
        if not server.allowed_tools:
            continue
        advertised: list[McpTool] = factory(server).list_tools()
        for tool in advertised:
            if tool.name in server.allowed_tools:
                out.append(
                    {
                        "server": server.name,
                        "tool": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                )
    return out


def _validate(
    settings: Settings,
    server_name: str,
    tool: str,
    *,
    client_factory: ClientFactory,
) -> tuple[McpServerConfig | None, str, list[str]]:
    """Shared checks for preview + run: server known, tool allowlisted + advertised.

    Returns ``(server, description, blockers)``; ``blockers`` is non-empty when the
    call can't proceed.
    """
    blockers: list[str] = []
    server = resolve_server(settings, server_name)
    if server is None:
        return None, "", [f'no MCP server named "{server_name}" is configured']
    if tool not in server.allowed_tools:
        return server, "", [f'the tool "{tool}" is not on {server_name}\'s allowlist']
    advertised = {t.name: t for t in client_factory(server).list_tools()}
    if tool not in advertised:
        blockers.append(f'the server "{server_name}" doesn\'t currently offer a "{tool}" tool')
    description = advertised[tool].description if tool in advertised else ""
    return server, description, blockers


def preview_action(
    settings: Settings,
    server_name: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    client_factory: ClientFactory | None = None,
) -> ActionPreview:
    """Validate and describe running ``{server}.{tool}(arguments)`` — no side effects."""
    factory = client_factory or _default_factory
    args = dict(arguments or {})
    server, description, blockers = _validate(settings, server_name, tool, client_factory=factory)
    digest = action_digest(server_name, tool, args) if server is not None else ""
    return ActionPreview(
        server=server_name,
        tool=tool,
        arguments=args,
        description=description,
        can_run=server is not None and not blockers,
        blockers=blockers,
        digest=digest,
    )


def run_action(
    store: MemoryStore,
    settings: Settings,
    server_name: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    expected_digest: str | None = None,
    client_factory: ClientFactory | None = None,
) -> ActionOutcome:
    """Run an allowlisted MCP tool after re-validating and matching the digest.

    Refuses with ``blocked`` if a precondition regressed (unknown server, tool no
    longer allowlisted/advertised) or ``stale`` if ``expected_digest`` no longer
    matches the call (``None`` opts out — an internal caller; an empty string is a
    *provided* value that can't match, so it's refused). On success/failure it logs
    an inert ``action`` audit episode and returns the tool's content.
    """
    factory = client_factory or _default_factory
    args = dict(arguments or {})
    server, _description, blockers = _validate(settings, server_name, tool, client_factory=factory)
    if server is None or blockers:
        return ActionOutcome(
            ran=False, code="blocked", server=server_name, tool=tool,
            detail=blockers[0] if blockers else "nothing to run",
        )
    if expected_digest is not None and expected_digest != action_digest(server_name, tool, args):
        return ActionOutcome(
            ran=False, code="stale", server=server_name, tool=tool,
            detail="the action changed since you previewed it — preview again before running",
        )
    result = factory(server).call_tool(tool, args)
    _audit(store, server_name, tool, ok=result.ok, detail=result.detail)
    if not result.ok:
        return ActionOutcome(
            ran=False, code="failed", server=server_name, tool=tool,
            content=result.content, detail=result.detail,
        )
    return ActionOutcome(
        ran=True, code="ran", server=server_name, tool=tool,
        content=result.content, detail=result.detail,
    )


def _audit(store: MemoryStore, server: str, tool: str, *, ok: bool, detail: str) -> None:
    """Leave a queryable trace of a run; best-effort, never fatal."""
    try:
        store.log_episode(
            ACTION_EPISODE,
            context=f"{server}.{tool}",
            notes=f"{'ok' if ok else 'failed'}: {detail}"[:500],
        )
    except Exception:  # audit is best-effort — a logging hiccup must not fail the run
        pass
