"""HTTP routes tagged "system".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
)
from fastapi.responses import (
    HTMLResponse,
    Response,
)

from prefrontal.stats import build_stats
from prefrontal.webhooks._common import (
    APP_ICON_PNG,
    APP_VERSION,
    DASHBOARD_HTML,
    FAMILY_HTML,
    KIDS_HTML,
    REVIEW_HTML,
    STATS_HTML,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "system" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness probe. Returns ``{"status": "ok"}`` with no auth required."""
        return {"status": "ok", "service": "prefrontal", "version": APP_VERSION}

    @router.get("/brand/app-icon.png", tags=["system"])
    def app_icon() -> Response:
        """Serve the PREFRONTAL app icon (PNG), unauthenticated.

        Referenced as the ``icon`` of every ntfy push so a notification renders
        as coming from the PREFRONTAL app. It must be fetchable with no auth (the
        recipient's ntfy app loads it with no token), which is why it's a public
        route — it carries no data, only the logo. Served from the box's own
        origin so it works for a private deployment; cached for a day.
        """
        return Response(
            content=APP_ICON_PNG,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/dashboard", response_class=HTMLResponse, tags=["system"])
    def dashboard() -> str:
        """Serve the read-only monitoring page (a self-contained HTML shell).

        The page itself is unauthenticated — it carries no data. It prompts for
        the ``X-Prefrontal-Token`` once (kept in the browser's localStorage) and
        sends it on every fetch to the auth-guarded ``GET`` endpoints
        (``/outings``, ``/todos``, ``/commitments``, ``/briefing``, ``/profile``),
        which it polls and refreshes. A prominent "Panic" button opens a focused
        overlay backed by ``/panic``. Reachable over Tailscale from any device.
        """
        return DASHBOARD_HTML

    @router.get("/family", response_class=HTMLResponse, tags=["system"])
    def family() -> str:
        """Serve the family view — a calm, read-only *shared household* glance.

        With households, the shared thing everyone can look at is the household
        sheet, so this is its read-only face: kids & facts, standing plans + star
        progress, the shopping list, upcoming appointments, recent changes, and
        (if enabled) the load-balance view — no edit forms (those live on
        ``/kids``). Like the other web surfaces the shell is unauthenticated and
        carries no data; it signs in via Google session or a one-time access code,
        then reads ``GET /household/sheet``. Shares the unified theme + nav.
        """
        return FAMILY_HTML

    @router.get("/kids", response_class=HTMLResponse, tags=["system"])
    def kids() -> str:
        """Serve the editable kids dashboard for the shared household sheet.

        Like the other web surfaces the shell is unauthenticated and carries no
        data; it prompts once for the access code (or rides a Google sign-in
        session) and then reads ``GET /household/sheet`` and writes through the
        household endpoints (facts / agreements / children / appointments) and the
        NL ``/assistant`` box. Reachable over Tailscale from any device.
        """
        return KIDS_HTML

    @router.get("/stats", response_class=HTMLResponse, tags=["system"])
    def stats_page() -> str:
        """Serve the behavioral Insights page — charts over the learning data.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then reads ``GET /stats/data`` and draws
        the time-estimation, follow-through, and channel-response charts with
        inline SVG/CSS. Shares the unified theme + nav.
        """
        return STATS_HTML

    @router.get("/review", response_class=HTMLResponse, tags=["system"])
    def review_page() -> str:
        """Serve the LLM-sensor review page — jot a note, confirm proposals.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then drives the sensor over the existing
        JSON API: ``POST /observe`` to turn a note into pending proposals, and
        ``GET /proposals`` + ``POST /proposals/{id}/accept|reject`` to review them.
        The human-in-the-loop path is unchanged — nothing is written until accept.
        Shares the unified theme + nav.
        """
        return REVIEW_HTML

    @router.get("/stats/data", tags=["system"])
    def stats_data(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Aggregated behavioral insights for the signed-in user (JSON for /stats)."""
        return build_stats(ctx.store)

    return router
