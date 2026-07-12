"""HTTP usage instrumentation — the ``invoked`` half of the feature-usage loop.

A pull feature (the panic screen, the morning briefing, the balance view, the
Insights page itself) leaves no behavioral episode behind — you just *look at it*.
This middleware is the one place that records those looks: on a successful GET to
a curated pull-surface route, it stamps one ``invoked`` event for the resolved
user, so :mod:`prefrontal.stats` can tell a feature you open weekly from one you
have never opened.

Deliberately an **observer**: it runs after the handler, only records on a 2xx
GET to a known surface, re-uses the existing auth resolver (never a second auth
scheme), and swallows everything — a telemetry write must never change what the
user sees. Unmatched routes (the ~200 write/POST endpoints, health checks) cost
nothing but a dict lookup.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from prefrontal.log import get_logger

logger = get_logger(__name__)

#: Exact GET path → the feature name it invokes. Kept curated (the human-facing
#: pull surfaces worth measuring), not every route — usage of a feature is a
#: deliberate look, not an incidental sub-resource fetch. Names line up with the
#: module registry where a surface maps to one (``trip_tracking``), else the
#: surface's own name (``panic``/``briefing``).
PULL_SURFACES: dict[str, str] = {
    "/dashboard": "dashboard",
    # The Insights page is a shell that fetches /stats/data; count the data read
    # (the real, authenticated view) rather than the shell to avoid double-counting.
    "/stats/data": "stats",
    "/panic": "panic",
    "/briefing": "briefing",
    "/balance": "balance",
    "/trips": "trip_tracking",
    "/profile": "profile",
    "/encouragement": "encouragement",
    "/clarifications": "clarify",
    "/calendar": "calendar",
    "/review": "review",
    "/kids": "household",
    "/family": "household",
    "/pets": "household",
    "/household": "household",
    "/projects/board": "projects",
    "/self-care": "self_care",
}


def feature_for_path(path: str) -> str | None:
    """Return the feature a GET ``path`` invokes, or ``None`` if it isn't a surface."""
    return PULL_SURFACES.get(path.rstrip("/") or "/")


async def usage_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Record an ``invoked`` usage event for a successful GET to a pull surface.

    The response is produced first and always returned unchanged; the stamp is a
    best-effort side effect wrapped so no failure (auth, DB, anything) can leak
    into the request path.
    """
    response = await call_next(request)
    try:
        if request.method != "GET" or response.status_code >= 400:
            return response
        feature = feature_for_path(request.url.path)
        if feature is None:
            return response
        # Reuse the real auth resolver so scoping is identical to the routes; a
        # request we can't attribute to a user is simply not recorded.
        from prefrontal.webhooks.deps import _resolve_user_row

        token = request.headers.get("x-prefrontal-token")
        user = _resolve_user_row(request, token)
        store = request.app.state.store.scoped(user["id"])
        store.record_feature_event(
            feature, "invoked", source="http", ref=request.url.path
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
        logger.debug("usage_middleware failed to record invocation", exc_info=True)
    return response
