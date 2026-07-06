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
    HTTPException,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)

from prefrontal.clock import utcnow
from prefrontal.modules.self_care import (
    apply_self_care_config,
    apply_self_care_mark,
    self_care_status,
)
from prefrontal.scheduling import local_datetime
from prefrontal.stats import build_stats
from prefrontal.webhooks._common import (
    APP_ICON_PNG,
    APP_VERSION,
    DASHBOARD_HTML,
    HOUSEHOLD_HTML,
    REVIEW_HTML,
    SETTINGS_HTML,
    STATS_HTML,
    lens_html,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import SelfCareConfig, SelfCareMark
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

    @router.get("/household", response_class=HTMLResponse, tags=["system"])
    def household() -> str:
        """Serve the editable Household hub — the one writable surface.

        Every edit to the shared sheet happens here: kids, pets, facts,
        agreements + star charts, shopping, routines, and the NL ``/assistant``
        box. Like the other web surfaces the shell is unauthenticated and carries
        no data; it prompts once for the access code (or rides a Google sign-in
        session), then reads ``GET /household/sheet`` and writes through the
        household endpoints. Reachable over Tailscale from any device.
        """
        return HOUSEHOLD_HTML

    @router.get("/kids", response_class=HTMLResponse, tags=["system"])
    def kids() -> str:
        """Serve the read-only **Kids** lens over the shared household sheet.

        A focused, calm view — roster & facts, standing plans + star progress,
        and upcoming appointments — with no edit forms (those live on
        ``/household``). Same self-contained, data-less shell as the other web
        surfaces; reads ``GET /household/sheet``.
        """
        return lens_html("kids")

    @router.get("/pets", response_class=HTMLResponse, tags=["system"])
    def pets() -> str:
        """Serve the read-only **Pets** lens over the shared household sheet.

        A focused view of the pet roster and their facts (meds, vet/groomer,
        food). Pets deliberately have no agreements/star charts (that's child
        scaffolding), so this lens is its own projection, not a relabeled Kids
        page. Read-only; edits live on ``/household``.
        """
        return lens_html("pets")

    @router.get("/family", tags=["system"])
    def family() -> RedirectResponse:
        """Retired: the whole-household overview now lives on ``/household``.

        Kept as a redirect so old bookmarks and links still land somewhere sane.
        """
        return RedirectResponse(url="/household", status_code=308)

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

    @router.get("/settings", response_class=HTMLResponse, tags=["system"])
    def settings_page() -> str:
        """Serve the Settings page — the one place for config that adjusts behavior.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then reads/writes the same JSON config
        endpoints the dashboard cards used to embed. Today it holds the self-care
        settings (master switch + each check's target, start hour, and cadence)
        via ``GET`` / ``POST /self-care`` — moved off the dashboard card so the
        card is purely "mark what I did today." Shares the unified theme + nav.
        """
        return SETTINGS_HTML

    @router.get("/stats/data", tags=["system"])
    def stats_data(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Aggregated behavioral insights for the signed-in user (JSON for /stats)."""
        return build_stats(ctx.store)

    @router.get("/self-care", tags=["system"])
    def self_care_data(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Today's self-care status for the signed-in user (JSON for the dashboard card).

        A read-only projection of the self-care coaching state — the master
        switch plus each check's progress toward its daily target — so the
        dashboard can show at a glance whether the checks are even on (the
        common "why am I not getting nudges?" cause) and where today stands.
        """
        return self_care_status(ctx.store, utcnow(), services.settings.timezone)

    @router.post("/self-care", tags=["system"])
    def set_self_care(
        payload: SelfCareConfig,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Adjust the signed-in user's self-care settings from the dashboard.

        A partial update — the master switch and/or any check's on/off, daily
        target, start hour, and cadence — so the card's controls can write the
        same coaching state that was previously sqlite-only. Returns the fresh
        status so the caller re-renders from the response.
        """
        apply_self_care_config(
            ctx.store,
            enabled=payload.enabled,
            checks={k: v.model_dump(exclude_none=True) for k, v in payload.checks.items()},
        )
        return self_care_status(ctx.store, utcnow(), services.settings.timezone)

    @router.post("/self-care/mark", tags=["system"])
    def mark_self_care(
        payload: SelfCareMark,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Log that the signed-in user did a self-care check today.

        Backs the dashboard card's "mark done" buttons — the way to record a
        check you completed after missing (or ignoring) its notification. Counts
        one toward the check's daily target with the same semantics as a one-tap
        notification confirm, then returns the fresh status (plus a short
        ``headline`` for feedback) so the card re-renders from the response.
        """
        now = utcnow()
        tz = services.settings.timezone
        today = local_datetime(now, tz).strftime("%Y-%m-%d")
        headline = apply_self_care_mark(ctx.store, payload.key, now=now, today=today)
        if headline is None:
            raise HTTPException(status_code=404, detail=f"unknown check: {payload.key}")
        return {"headline": headline, **self_care_status(ctx.store, now, tz)}

    return router
