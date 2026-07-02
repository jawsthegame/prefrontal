"""HTTP routes tagged "system".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    APP_VERSION,
    DASHBOARD_HTML,
    FAMILY_HTML,
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
        """Serve the family view — a calm, non-technical, read-only page.

        Like ``/dashboard`` the shell is unauthenticated and carries no data; it
        prompts once for the access code (the ``X-Prefrontal-Token``) and polls
        only the gentle, read-only endpoints (``/outings`` for "right now" and
        ``/briefing`` for today's plan), plus a soft "Feeling overwhelmed?" button
        that opens a focused ``/panic`` overlay. No levels, profile, or action
        buttons — meant for a partner to glance at over Tailscale.
        """
        return FAMILY_HTML

    return router
