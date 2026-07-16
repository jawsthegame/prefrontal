"""HTTP route tagged "mcp" — Prefrontal's built-in MCP server endpoint (roadmap M4).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes
``POST /mcp``: a JSON-RPC endpoint speaking MCP (``initialize`` / ``tools/list`` /
``tools/call``) so an external agent, the delegation agent, or Prefrontal's own
action client can call the bounded tools in :mod:`prefrontal.mcp_server`. Auth is
the caller's per-user token (every tool acts on that user's own scoped data);
operator-only tools (e.g. ``place_call``) are refused for non-operators inside the
dispatcher.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Request,
    Response,
    status,
)

from prefrontal.mcp_server import ToolContext, handle_rpc
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "mcp" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings

    @router.post("/mcp", tags=["mcp"])
    def mcp_rpc(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        body: Annotated[Any, Body()] = None,
    ):
        """Serve one MCP JSON-RPC message against the caller's scoped data.

        A request returns its JSON-RPC response; a notification (no ``id``) returns
        202 with no body. Tool-level failures come back as an MCP ``isError`` result
        (HTTP 200), not an HTTP error, per the protocol. The body is accepted as
        arbitrary JSON (not forced to an object) so a non-object gets the proper
        JSON-RPC ``-32700`` from :func:`handle_rpc` rather than a FastAPI 422.
        """
        tool_ctx = ToolContext(
            store=ctx.store,
            settings=resolved_settings,
            is_operator=bool(ctx.user.get("is_operator")),
            voice_client=getattr(request.app.state, "mcp_voice_client", None),
        )
        response = handle_rpc(tool_ctx, body)
        if response is None:
            return Response(status_code=status.HTTP_202_ACCEPTED)
        return response

    return router
