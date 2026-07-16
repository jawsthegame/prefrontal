"""HTTP routes tagged "actions" — scoped MCP tool-calls (roadmap M4).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes the
generic "does the thing" provider: list the allowlisted tools on the configured
MCP servers, preview a call, and — behind the same preview→confirm gate as the
delegation email-send — run one. All the safety (allowlist, digest, audit) lives
in :mod:`prefrontal.actions`; this is a thin HTTP skin over it.

When no MCP server is configured the capability is dormant: ``/actions/tools``
returns an empty list and preview/run report the server is unknown.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)

from prefrontal.actions import (
    list_available_tools,
    preview_action,
    run_action,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    ActionPreviewRequest,
    ActionRunRequest,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "actions" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings

    def _factory(request):
        """Use an injected test factory (app.state) if present, else the real client."""
        return getattr(request.app.state, "mcp_client_factory", None)

    @router.get("/actions/tools", tags=["actions"])
    def actions_tools(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """List the callable (configured + allowlisted) MCP tools across servers.

        Empty when no MCP server is configured — the capability is opt-in and
        local-first. Read-only.
        """
        tools = list_available_tools(resolved_settings, client_factory=_factory(request))
        return {"tools": tools}

    @router.post("/actions/preview", tags=["actions"])
    def actions_preview(
        payload: ActionPreviewRequest,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Dry-run a scoped tool-call: validate it and return a digest — no side effects.

        ``can_run`` is the go/no-go; ``blockers`` explains any no (unknown server,
        tool not allowlisted, tool not offered). Echo ``digest`` back to
        ``POST /actions/run`` to confirm.
        """
        preview = preview_action(
            resolved_settings,
            payload.server,
            payload.tool,
            payload.arguments,
            client_factory=_factory(request),
        )
        return {
            "server": preview.server,
            "tool": preview.tool,
            "arguments": preview.arguments,
            "description": preview.description,
            "can_run": preview.can_run,
            "blockers": preview.blockers,
            "digest": preview.digest,
        }

    @router.post("/actions/run", tags=["actions"])
    def actions_run(
        payload: ActionRunRequest,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Confirm and run a scoped tool-call.

        Re-validates against the current config and refuses a stale call (409 — the
        ``preview_digest`` no longer matches) or a structural blocker (422), then
        calls the tool. A tool/transport rejection returns 200 with ``ran: false``
        and the reason (the "report, never raise" ethos); a success returns the
        tool's ``content``. Every run leaves an inert ``action`` audit episode.
        """
        outcome = run_action(
            ctx.store,
            resolved_settings,
            payload.server,
            payload.tool,
            payload.arguments,
            expected_digest=payload.preview_digest,
            client_factory=_factory(request),
        )
        if outcome.code == "stale":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=outcome.detail)
        if outcome.code == "blocked":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=outcome.detail
            )
        return {
            "server": outcome.server,
            "tool": outcome.tool,
            "ran": outcome.ran,
            "content": outcome.content,
            "detail": outcome.detail,
        }

    return router
