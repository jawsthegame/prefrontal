"""Constants shared by the webhook layer.

The routers used to import *everything* through this module — Pydantic models,
request dependencies, formatting helpers, and a wide re-export barrel of domain
functions. Those now live in their own modules
(:mod:`~prefrontal.webhooks.schemas`, :mod:`~prefrontal.webhooks.deps`,
:mod:`~prefrontal.webhooks.helpers`) and each router imports domain functions
straight from the domain package. What remains here is the handful of constants
with no better home: the shortcut-action → episode-outcome map, the self-contained
HTML pages (read once at import), and the app version / inference timeout.
"""

from __future__ import annotations

from pathlib import Path

#: Maps a one-tap shortcut action to the resulting ``episodes.outcome`` value.
ACTION_OUTCOME: dict[str, str] = {
    "made_it": "success",
    "missed_it": "miss",
    "partial": "partial",
}

#: The shared card-column layout (draggable/collapsible columns), read once and
#: injected into every page that opts in with a ``.masonry[data-layout-key]``
#: container. Kept in standalone .css/.js files (editor tooling) but folded into
#: each page at import so the served HTML stays self-contained — the same reason
#: the pages inline everything else.
_CARD_LAYOUT_CSS = (Path(__file__).with_name("_card_layout.css")).read_text(encoding="utf-8")
_CARD_LAYOUT_JS = (Path(__file__).with_name("_card_layout.js")).read_text(encoding="utf-8")


def _with_card_layout(html: str) -> str:
    """Fold the shared card-layout CSS/JS into a page at its ``<!--CARD_LAYOUT_*-->``
    tokens. A page without the tokens is returned unchanged."""
    return (
        html.replace("<!--CARD_LAYOUT_CSS-->", _CARD_LAYOUT_CSS)
        .replace("<!--CARD_LAYOUT_JS-->", f"<script>\n{_CARD_LAYOUT_JS}\n</script>")
    )


#: The shared nav-reveal script that shows the operator-only "Admin" link (which
#: ships hidden in every nav) once ``GET /admin/whoami`` confirms the signed-in
#: user is an operator. Injected into every shared-nav page below so the logic
#: lives in one file, not copied into each shell.
_ADMIN_NAV_JS = (Path(__file__).with_name("_admin_nav.js")).read_text(encoding="utf-8")


def _shell(name: str) -> str:
    """Read a self-contained page shell and reveal its operator-only Admin link.

    Every shared-nav page authors the ``<a data-nav-admin>`` link hidden and gets
    the one shared reveal script injected before ``</body>``, so the operator-gating
    logic lives in a single file rather than being copied into each shell.
    """
    html = (Path(__file__).with_name(name)).read_text(encoding="utf-8")
    return html.replace("</body>", f"<script>\n{_ADMIN_NAV_JS}\n</script>\n</body>", 1)


#: The self-contained monitoring page, read once at import (like ``schema.sql``).
DASHBOARD_HTML = _shell("dashboard.html")
#: The read-only visual household calendar (week-ahead agenda + slot finder);
#: reads GET /commitments and GET /calendar/slots.
CALENDAR_HTML = _shell("calendar.html")
#: The editable household hub — the one writable surface for the shared sheet
#: (kids, pets, facts, agreements, shopping, routines).
HOUSEHOLD_HTML = _with_card_layout(_shell("household.html"))
#: The read-only lens shell, parameterized per focus by replacing ``__LENS__``
#: with ``kids`` / ``pets`` (see :func:`lens_html`). One file backs both lenses.
LENS_HTML = _with_card_layout(_shell("lens.html"))


def lens_html(lens: str) -> str:
    """The read-only lens shell wired to a specific focus (``kids`` / ``pets``).

    The shell carries a ``__LENS__`` token the client reads to pick which slices
    of ``GET /household/sheet`` to render; we bind it server-side so each route
    (``/kids``, ``/pets``) serves a self-contained page. ``lens`` is a fixed
    internal literal, never user input.
    """
    return LENS_HTML.replace("__LENS__", lens)
#: The behavioral Insights page (charts over episodes; reads GET /stats/data).
STATS_HTML = _with_card_layout(_shell("stats.html"))
#: The LLM-sensor review page (jot a note → confirm proposals; reads/writes
#: GET /proposals + POST /observe + POST /proposals/{id}/accept|reject).
REVIEW_HTML = _with_card_layout(_shell("review.html"))
#: The Settings page — config that adjusts behavior (currently the self-care
#: master switch + per-check knobs), reading/writing GET + POST /self-care.
SETTINGS_HTML = _with_card_layout(_shell("settings.html"))
#: The operator-only user-management page — provision users (token shown once),
#: rotate/disable them, create households, and wire members in. Reads/writes the
#: ``/admin/*`` endpoints, all guarded by ``require_operator``.
ADMIN_HTML = (Path(__file__).with_name("admin.html")).read_text(encoding="utf-8")

#: The PREFRONTAL app icon (PNG bytes), read once at import and served
#: unauthenticated at ``GET /brand/app-icon.png`` so an ntfy push can reference
#: it as its notification ``icon`` — this is what makes a push render as coming
#: from the PREFRONTAL app. Served from the box's own (Tailscale) origin, the
#: same origin the phone already reaches for one-tap action buttons, so it works
#: for a private deployment where a public GitHub raw URL would 404.
APP_ICON_PNG = (Path(__file__).with_name("app-icon.png")).read_bytes()


INFER_TIMEOUT_SECONDS = 10.0

APP_VERSION = "0.1.0"
