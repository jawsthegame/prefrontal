"""HTTP routes tagged "system".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    APP_VERSION,
    DASHBOARD_HTML,
    FAMILY_HTML,
    KIDS_HTML,
    HTMLResponse,
)


def build_router(
    *,
    resolved_settings,
    n8n,
    ollama_client,
    summarizer_client,
    geocoder_client,
    _run_geocode,
) -> APIRouter:
    """Build the "system" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness probe. Returns ``{"status": "ok"}`` with no auth required."""
        return {"status": "ok", "service": "prefrontal", "version": APP_VERSION}

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

    return router
