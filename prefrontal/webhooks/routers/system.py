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

from prefrontal.clock import local_datetime, utcnow
from prefrontal.integrations.nominatim import GeocoderError
from prefrontal.modules import available, enabled_modules
from prefrontal.modules.registry import MODULE_ENABLED_PREFIX, user_disabled_module_keys
from prefrontal.modules.self_care import (
    apply_self_care_config,
    apply_self_care_mark,
    apply_self_care_reset,
    apply_self_care_unmark,
    self_care_status,
)
from prefrontal.packs.registry import PACK_ENABLED_PREFIX, enabled_packs, user_disabled_pack_keys
from prefrontal.packs.registry import available as available_packs
from prefrontal.self_care_review import self_care_review
from prefrontal.sources import (
    SMTP_ACCOUNT,
    delete_smtp_source,
    imap_accounts,
    put_smtp_source,
    resolve_smtp,
    smtp_sources,
)
from prefrontal.stats import build_stats
from prefrontal.webhooks._common import (
    ADMIN_HTML,
    APP_ICON_PNG,
    APP_VERSION,
    CALENDAR_HTML,
    CARE_HTML,
    DASHBOARD_HTML,
    DAY_HTML,
    GUIDE_HTML,
    HOUSEHOLD_HTML,
    PROJECTS_HTML,
    REVIEW_HTML,
    SETTINGS_HTML,
    STATS_HTML,
    TRIPS_HTML,
    lens_html,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    ApnsTokenRegistration,
    FeatureToggle,
    GeocodingToggle,
    GuideProgress,
    SelfCareConfig,
    SelfCareMark,
    SmtpConfig,
)
from prefrontal.webhooks.schemas.ingestion import HomeSet
from prefrontal.webhooks.services import RouterServices

#: The web-surface HTML shells are read into memory at import and change only on
#: deploy. Without this, a browser can keep serving a stale copy after an update
#: (the shells carry no validators), so a deploy appears to "not take". ``no-cache``
#: forces the browser to revalidate every load; since the shells have no ETag the
#: revalidation always refetches, i.e. the newest shell always wins. They carry no
#: user data, so there's nothing sensitive to keep out of cache.
_PAGE_NO_CACHE = {"Cache-Control": "no-cache"}


def _page(html: str) -> HTMLResponse:
    """An HTML shell response that a browser must revalidate (never serve stale)."""
    return HTMLResponse(html, headers=_PAGE_NO_CACHE)


#: Coaching-state key holding the set of modules whose Guide walkthrough the user
#: has marked read, as a comma-separated list of module keys. Read-your-own-progress
#: only — it gates nothing and is cleared wholesale by ``POST /guide/reset``.
_GUIDE_SEEN_KEY = "guide_seen"


def _guide_seen(store: Any) -> set[str]:
    """The set of module keys the signed-in user has marked read in the Guide."""
    raw = store.get_state(_GUIDE_SEEN_KEY) or ""
    return {k for k in raw.split(",") if k}


def _write_guide_seen(store: Any, keys: set[str]) -> None:
    """Persist the marked-read set (sorted, deduped) back to coaching state."""
    store.set_state(_GUIDE_SEEN_KEY, ",".join(sorted(keys)), source="explicit")


def _apply_overrides(
    store: Any, overrides: dict[str, bool], known: set[str], prefix: str
) -> None:
    """Write per-user on/off overrides for the given keys (Settings "Features").

    ``True`` clears the override (back to the deployment default); ``False`` sets
    ``<prefix><key> = "off"``. Keys not in ``known`` (unregistered) are skipped.
    """
    for key, on in overrides.items():
        if key not in known:
            continue
        if on:
            store.delete_state(f"{prefix}{key}")
        else:
            store.set_state(f"{prefix}{key}", "off", source="explicit")


def _guide_payload(store: Any, settings: Any) -> dict[str, Any]:
    """Assemble the Guide's JSON — one entry per *enabled* module, plus progress.

    Built from each module's own ``tutorial()`` and ``interventions()`` so the
    walkthrough always matches what the deployment actually runs (a disabled
    module never appears), and adding a module surfaces its guide automatically.
    """
    seen = _guide_seen(store)
    modules = [
        {
            "key": m.key,
            "title": m.title,
            "challenge": m.challenge,
            "completed": m.key in seen,
            "steps": [{"title": s.title, "body": s.body} for s in m.tutorial()],
            "interventions": [
                {
                    "name": i.name,
                    "description": i.description,
                    "trigger": i.trigger,
                    "status": i.status,
                }
                for i in m.interventions()
            ],
        }
        for m in enabled_modules(settings)
    ]
    live = {m["key"] for m in modules}
    return {
        "modules": modules,
        "completed": sorted(seen & live),
        "total": len(modules),
    }


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
    def dashboard() -> HTMLResponse:
        """Serve the read-only monitoring page (a self-contained HTML shell).

        The page itself is unauthenticated — it carries no data. It prompts for
        the ``X-Prefrontal-Token`` once (kept in the browser's localStorage) and
        sends it on every fetch to the auth-guarded ``GET`` endpoints
        (``/outings``, ``/todos``, ``/commitments``, ``/briefing``, ``/profile``),
        which it polls and refreshes. A prominent "Panic" button opens a focused
        overlay backed by ``/panic``. Reachable over Tailscale from any device.
        """
        return _page(DASHBOARD_HTML)

    @router.get("/calendar", response_class=HTMLResponse, tags=["system"])
    def calendar() -> HTMLResponse:
        """Serve the read-only visual household calendar (a self-contained HTML shell).

        A calm week-ahead agenda over the caller's commitments (``GET
        /commitments``), plus a "find a free slot" control that queries ``GET
        /calendar/slots`` and highlights the open windows of the requested length
        inline in their day. Like the other web surfaces the shell is
        unauthenticated and carries no data — it signs in via Google session or an
        access code, then reads the auth-guarded JSON endpoints. Read-only: events
        are edited in the user's own calendar app and sync in.
        """
        return _page(CALENDAR_HTML)

    @router.get("/projects/board", response_class=HTMLResponse, tags=["system"])
    def projects_board() -> HTMLResponse:
        """Serve the **Projects** page — a dedicated lens over the user's projects.

        A master/detail view: the projects grouped by domain on the left, and the
        selected project's open todos, upcoming commitments, recent focus sessions,
        and rollup stats on the right — with an "include archived" toggle and
        ``#p<id>`` deep-linking to a specific project. Same self-contained, data-less
        shell as the other web surfaces; reads ``GET /projects`` and
        ``GET /projects/{id}``.

        Served at ``/projects/board`` rather than ``/projects`` because the bare
        ``/projects`` path is the JSON list endpoint; this literal route is
        registered ahead of the API's ``/projects/{project_id}`` so it isn't
        swallowed by that int-typed path param.
        """
        return _page(PROJECTS_HTML)

    @router.get("/household", response_class=HTMLResponse, tags=["system"])
    def household() -> HTMLResponse:
        """Serve the editable Household hub — the one writable surface.

        Every edit to the shared sheet happens here: kids, pets, facts,
        agreements + star charts, shopping, routines, and the NL ``/assistant``
        box. Like the other web surfaces the shell is unauthenticated and carries
        no data; it prompts once for the access code (or rides a Google sign-in
        session), then reads ``GET /household/sheet`` and writes through the
        household endpoints. Reachable over Tailscale from any device.
        """
        return _page(HOUSEHOLD_HTML)

    @router.get("/kids", response_class=HTMLResponse, tags=["system"])
    def kids() -> HTMLResponse:
        """Serve the read-only **Kids** lens over the shared household sheet.

        A focused, calm view — roster & facts, standing plans + star progress,
        and upcoming appointments — with no edit forms (those live on
        ``/household``). Same self-contained, data-less shell as the other web
        surfaces; reads ``GET /household/sheet``.
        """
        return _page(lens_html("kids"))

    @router.get("/care", response_class=HTMLResponse, tags=["system"])
    def care() -> HTMLResponse:
        """Serve the read-only **Care** surface — the caregiver counterpart to /kids.

        Upcoming ``care`` appointments (for the adult you look after) and your open
        medical/admin/caregiving todos. Gated in the data endpoint on the Caregiver
        context pack (``PREFRONTAL_PACKS=caregiver``), not household membership — a
        solo caregiver isn't a co-parent; the page prompts to enable it when off.
        Same self-contained, data-less shell as the other web surfaces; reads
        ``GET /care/sheet``.
        """
        return _page(CARE_HTML)

    @router.get("/pets", response_class=HTMLResponse, tags=["system"])
    def pets() -> HTMLResponse:
        """Serve the read-only **Pets** lens over the shared household sheet.

        A focused view of the pet roster and their facts (meds, vet/groomer,
        food). Pets deliberately have no agreements/star charts (that's child
        scaffolding), so this lens is its own projection, not a relabeled Kids
        page. Read-only; edits live on ``/household``.
        """
        return _page(lens_html("pets"))

    @router.get("/family", tags=["system"])
    def family() -> RedirectResponse:
        """Retired: the whole-household overview now lives on ``/household``.

        Kept as a redirect so old bookmarks and links still land somewhere sane.
        """
        return RedirectResponse(url="/household", status_code=308)

    @router.get("/stats", response_class=HTMLResponse, tags=["system"])
    def stats_page() -> HTMLResponse:
        """Serve the behavioral Insights page — charts over the learning data.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then reads ``GET /stats/data`` and draws
        the time-estimation, follow-through, and channel-response charts with
        inline SVG/CSS. Shares the unified theme + nav.
        """
        return _page(STATS_HTML)

    @router.get("/review", response_class=HTMLResponse, tags=["system"])
    def review_page() -> HTMLResponse:
        """Serve the LLM-sensor review page — jot a note, confirm proposals.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then drives the sensor over the existing
        JSON API: ``POST /observe`` to turn a note into pending proposals, and
        ``GET /proposals`` + ``POST /proposals/{id}/accept|reject`` to review them.
        The human-in-the-loop path is unchanged — nothing is written until accept.
        Shares the unified theme + nav.
        """
        return _page(REVIEW_HTML)

    @router.get("/settings", response_class=HTMLResponse, tags=["system"])
    def settings_page() -> HTMLResponse:
        """Serve the Settings page — the one place for config that adjusts behavior.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then reads/writes the same JSON config
        endpoints the dashboard cards used to embed. Today it holds the self-care
        settings (master switch + each check's target, start hour, and cadence)
        via ``GET`` / ``POST /self-care`` — moved off the dashboard card so the
        card is purely "mark what I did today." Shares the unified theme + nav.
        """
        return _page(SETTINGS_HTML)

    @router.get("/trips/board", response_class=HTMLResponse, tags=["system"])
    def trips_board() -> HTMLResponse:
        """Serve the **Trips & balance** page — the visual surface for trip tracking.

        Closed-loop trips are otherwise only surfaced through notifications, the
        briefing, and the raw JSON API; this page renders them: the open trip (if
        you're out now), the recent history with label/category/outcome, the trips
        still awaiting a name, and the life-sphere **focus-balance** rollup. Same
        self-contained, data-less shell as the other web surfaces; reads
        ``GET /trips`` and ``GET /balance``. Read-only — trips are labeled from the
        one-tap notification the ``trip_tracking`` module sends.

        Served at ``/trips/board`` because the bare ``/trips`` path is the JSON list
        endpoint (same split as ``/projects/board`` vs ``/projects``).
        """
        return _page(TRIPS_HTML)

    @router.get("/day/board", response_class=HTMLResponse, tags=["system"])
    def day_board() -> HTMLResponse:
        """Serve the **Today's shape** page — the day as a proportional timeline.

        The visual surface for the day-shape: today's commitments as fixed blocks,
        the open todos fitted into the forward gaps, and the free time in between,
        each block's height tracking its length so the day reads as a *shape* (the
        Structured / Tiimo pattern). Designed for how ADHD readers actually read a
        chart — every block carries a non-colour signal (a kind word + glyph + a
        solid/dashed/dotted edge), so it stays legible without leaning on hue
        (CHI-2024). Same self-contained, data-less shell as the other web surfaces;
        reads ``GET /day``. Read-only and forgiving — the past is dimmed, not scored.

        Served at ``/day/board`` because the bare ``/day`` path is the JSON endpoint
        (same split as ``/trips/board`` vs ``/trips`` and ``/projects/board``).
        """
        return _page(DAY_HTML)

    @router.get("/guide", response_class=HTMLResponse, tags=["system"])
    def guide_page() -> HTMLResponse:
        """Serve the new-user Guide — a walkthrough of each enabled module.

        Self-contained shell (unauthenticated, carries no data); it signs in via
        Google session or an access code, then reads ``GET /guide/data`` and walks
        the caller through what each of *their* enabled modules does, step by step.
        Each module can be marked "Got it" (``POST /guide/seen``) to track how far
        a new user has got, and the whole thing reset (``POST /guide/reset``) so it
        can be re-read any time — the tour is never a one-shot. Shares the unified
        theme + nav.
        """
        return _page(GUIDE_HTML)

    @router.get("/manual", response_class=HTMLResponse, tags=["system"])
    def usage_guide_page() -> HTMLResponse:
        """Serve the full **usage guide** — `docs/guide.md`, rendered as a web page.

        Where ``/guide`` is the per-module new-user walkthrough (and ``/docs`` is
        FastAPI's API explorer), this is the whole practical manual: what each
        capability does, the problem it solves, and runnable examples. Rendered on
        request from the single Markdown source of truth in the repo, so the page
        never drifts from the doc. Unauthenticated and data-free like the other web
        surfaces (the guide carries no personal data), and reachable over Tailscale.
        Degrades gracefully if the Markdown renderer or the source file is
        unavailable (see :mod:`prefrontal.webhooks.usage_guide`).
        """
        from prefrontal.webhooks.usage_guide import render_usage_guide_page

        return _page(render_usage_guide_page())

    @router.get("/admin", response_class=HTMLResponse, tags=["system"])
    def admin_page() -> HTMLResponse:
        """Serve the operator-only user-management page.

        Self-contained shell (unauthenticated, carries no data); it signs in with
        an **operator** access code, then drives the ``/admin/*`` endpoints —
        provision a user (token shown once), rotate/disable, create a household,
        and wire members in. Those endpoints are all guarded by
        ``require_operator``, so a non-operator who reaches this page just can't
        read or write anything. Shares the unified theme + nav.
        """
        return _page(ADMIN_HTML)

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

    @router.get("/self-care/review", tags=["system"])
    def self_care_review_data(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Today's end-of-day self-care **gap** analysis for the signed-in user.

        The pull twin of the opt-in evening push: reads today's confirms back as a
        timeline and surfaces the gaps a raw count hides — a late first glass of
        water, a long stretch between bio breaks, a quota finished short — plus
        what went well. A pure read (no writes), safe to poll any time.
        """
        return self_care_review(ctx.store, utcnow(), services.settings.timezone)

    def _features_view(store: Any) -> dict[str, Any]:
        """The signed-in user's per-module and per-pack on/off state.

        Lists every **deployment-enabled** module and pack (the sets the user can
        turn off for themselves), each with its title, a one-line blurb, and
        whether it's currently on for them. Turning a pack off is a *surfaces*
        overlay (its situation tools + ``/care`` lens). Read-only.
        """
        mod_off = user_disabled_module_keys(store)
        pack_off = user_disabled_pack_keys(store)
        return {
            "modules": [
                {
                    "key": m.key,
                    "title": m.title,
                    "challenge": m.challenge,
                    "enabled": m.key not in mod_off,
                }
                for m in enabled_modules(services.settings)
            ],
            "packs": [
                {
                    "key": p.key,
                    "title": p.title,
                    "description": p.description,
                    "enabled": p.key not in pack_off,
                }
                for p in enabled_packs(services.settings)
            ],
        }

    @router.get("/settings/features", tags=["system"])
    def get_features(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Per-user feature toggles — which modules and packs the user has on.

        The read side of the Settings "Features" control: the deployment-enabled
        modules and packs, each flagged with whether this user has turned it off.
        A per-user overlay (``module_enabled:<key>`` / ``pack_enabled:<key>``
        coaching state); deployment config is never changed. Read-only.
        """
        return _features_view(ctx.store)

    @router.post("/settings/features", tags=["system"])
    def set_features(
        payload: FeatureToggle,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Turn modules/packs on/off for the signed-in user (Settings "Features").

        For each ``{key: on}`` pair: ``true`` clears the override (back to the
        deployment default), ``false`` disables it for this user only — a disabled
        module offers no cues or protection in the coaching tick; a disabled pack
        hides its situation tools and ``/care`` lens. Only registered keys take
        effect (others are ignored). Returns the fresh feature view.
        """
        # Accept any *registered* key (matches the resolvers, which scan the full
        # registry): storing an override for a deployment-disabled module/pack is
        # inert until/unless the operator enables it, at which point the user's
        # prior choice holds.
        _apply_overrides(
            ctx.store, payload.modules, {m.key for m in available()}, MODULE_ENABLED_PREFIX
        )
        _apply_overrides(
            ctx.store, payload.packs, {p.key for p in available_packs()}, PACK_ENABLED_PREFIX
        )
        return _features_view(ctx.store)

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
            review=payload.review.model_dump(exclude_none=True) if payload.review else None,
        )
        return self_care_status(ctx.store, utcnow(), services.settings.timezone)

    @router.post("/self-care/mark", tags=["system"])
    def mark_self_care(
        payload: SelfCareMark,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Log that the signed-in user did a self-care check today.

        Backs the dashboard chips — a plain tap records a check you completed
        (counts one toward its daily target, same semantics as a one-tap
        notification confirm); a shift-click sends ``undo`` to rewind an accidental
        count by one (floored at zero); and ``reset`` cycles the count back to zero
        once it's at the target — the mobile-friendly wrap-around, since touch has
        no shift-click. Returns the fresh status (plus a short ``headline`` for
        feedback) so the card re-renders from it.
        """
        now = utcnow()
        tz = services.settings.timezone
        today = local_datetime(now, tz).strftime("%Y-%m-%d")
        if payload.reset:
            headline = apply_self_care_reset(ctx.store, payload.key, today=today)
        elif payload.undo:
            headline = apply_self_care_unmark(ctx.store, payload.key, today=today)
        else:
            headline = apply_self_care_mark(ctx.store, payload.key, now=now, today=today)
        if headline is None:
            raise HTTPException(status_code=404, detail=f"unknown check: {payload.key}")
        return {"headline": headline, **self_care_status(ctx.store, now, tz)}

    @router.get("/guide/data", tags=["system"])
    def guide_data(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The new-user walkthrough for the signed-in user's enabled modules (JSON).

        Backs the ``/guide`` page: each enabled module's step-by-step tour plus the
        caller's own read-progress. Read-only — reading the guide records nothing.
        """
        return _guide_payload(ctx.store, services.settings)

    @router.post("/guide/seen", tags=["system"])
    def mark_guide_seen(
        payload: GuideProgress,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Mark one module's walkthrough read (or new again) for the signed-in user.

        Backs the per-module "Got it ✓ / Mark as new" toggle. Idempotent, and a
        no-op semantically for anything but progress display — a user can flip a
        module back to new and re-read it as often as they like. Returns the fresh
        payload so the caller re-renders from it.
        """
        seen = _guide_seen(ctx.store)
        if payload.seen:
            seen.add(payload.key)
        else:
            seen.discard(payload.key)
        _write_guide_seen(ctx.store, seen)
        return _guide_payload(ctx.store, services.settings)

    @router.post("/guide/reset", tags=["system"])
    def reset_guide(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Clear the signed-in user's Guide progress — start the tour over.

        The "re-do" affordance: marks every module's walkthrough unread again so
        the Guide reads as fresh. Returns the reset payload.
        """
        _write_guide_seen(ctx.store, set())
        return _guide_payload(ctx.store, services.settings)

    def _home_payload(store: Any) -> dict[str, Any]:
        """The home coordinate + whether external address lookup is opted in."""
        return {
            "home": store.get_home(),
            "geocoding_enabled": store.get_bool("geocoding_enabled", False),
        }

    @router.get("/home", tags=["system"])
    def home_get(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The signed-in user's home coordinate (or null) + geocoding opt-in state.

        Backs the Settings "Home location" card and lets the Trips page tell whether
        trip detection is armed (it stays dormant until a home coordinate is set).
        """
        return _home_payload(ctx.store)

    @router.post("/home", tags=["system"])
    def home_set(
        payload: HomeSet,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set the home coordinate from the web UI (same store as ``POST /webhooks/home``).

        The web counterpart to the iOS ``/webhooks/home`` — arms closed-loop trip
        detection. The coordinate can come from the browser's own geolocation, a
        picked address (see ``GET /geocode/search``), or a manual lat/lon. Returns
        the fresh payload so the card re-renders from it.
        """
        ctx.store.set_home(payload.lat, payload.lon)
        return _home_payload(ctx.store)

    @router.post("/home/geocoding", tags=["system"])
    def set_geocoding(
        payload: GeocodingToggle,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Opt in/out of external address lookup (the ``geocoding_enabled`` flag).

        Address search sends the typed text off-host to the configured geocoder, so
        it's off by default; the card's checkbox flips it here. Returns the fresh
        home payload.
        """
        # Stored as "1"/"0" to match the rest of the codebase — the seed default
        # (_helpers.py) and tests use 1/0, and manual DB edits expect it — even
        # though get_bool() would also accept true/false.
        ctx.store.set_state(
            "geocoding_enabled", "1" if payload.enabled else "0", source="explicit"
        )
        return _home_payload(ctx.store)

    @router.get("/geocode/search", tags=["system"])
    def geocode_search(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        q: str = "",
    ) -> dict[str, Any]:
        """Resolve a typed address to a list of real-place candidates (for the picker).

        A deliberate, user-initiated lookup (Search button, not as-you-type) so it
        respects the geocoder's usage policy. Gated on the ``geocoding_enabled``
        opt-in — when off, returns ``{"enabled": false, "results": []}`` and never
        touches the network. Each result is ``{display_name, lat, lon}``.
        """
        if not ctx.store.get_bool("geocoding_enabled", False):
            return {"enabled": False, "results": []}
        query = q.strip()
        if not query:
            return {"enabled": True, "results": []}
        try:
            results = services.geocoder.search(query, limit=5)
        except GeocoderError as exc:
            raise HTTPException(status_code=502, detail=f"Address lookup failed: {exc}") from exc
        return {"enabled": True, "results": results}

    @router.post("/route/apns-token", tags=["system"])
    def register_apns_token(
        payload: ApnsTokenRegistration,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Register (or clear) this device's APNs token for native iOS push.

        The app posts the token from ``registerForRemoteNotifications``; it's
        stored as the user's per-user ``apns_token`` route so nudges deliver via
        Apple Push (falling back to ntfy when APNs isn't configured or the send
        fails). An empty ``token`` clears it — unregister on sign-out. The token
        itself is never echoed back.
        """
        token = payload.token.strip()
        ctx.store.set_state("apns_token", token, source="explicit")
        return {"registered": bool(token)}

    def _smtp_entry(src: Any) -> dict[str, Any]:
        """Serialize one SMTP source for the client — the password is NEVER included.

        ``password_set`` says whether a secret is stored, so the form can show
        "•••• (saved)" without the browser ever seeing the sealed value.
        """
        return {
            "account": src.account,
            "host": src.host,
            "port": src.port,
            "username": src.username,
            "sender": src.sender,
            "use_tls": src.use_tls,
            "enabled": src.enabled,
            "configured": src.configured,
            "password_set": bool(src.password),
        }

    @router.get("/smtp", tags=["system"])
    def smtp_data(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The signed-in user's outbound-email (SMTP) accounts, for the Settings card.

        Powers the email hand-off for delegated todos. A user may keep several
        accounts keyed by name — a delegated todo sends from the one matching its
        mail account or domain (else ``default``). ``mail_accounts`` lists the
        user's IMAP account names so the UI can suggest matching outbox names, and
        every password is sealed at rest and **never** returned (see
        :func:`_smtp_entry`).
        """
        return {
            "accounts": [_smtp_entry(s) for s in smtp_sources(ctx.store)],
            "mail_accounts": imap_accounts(ctx.store, include_disabled=True),
            "default_account": SMTP_ACCOUNT,
        }

    @router.post("/smtp", tags=["system"])
    def set_smtp(
        payload: SmtpConfig,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Create or update one SMTP account (used to email delegated todos to a VA).

        ``account`` names the outbox (defaults to ``default``; name it after a mail
        account or domain — ``work``/``personal`` — for auto-matched sending). A
        partial update: a blank/omitted ``password`` keeps the stored one (``None``
        into :func:`~prefrontal.sources.put_smtp_source` preserves the sealed
        secret), so editing the host doesn't require retyping the app password.
        Returns the fresh (password-free) entry so the card re-renders from it.
        """
        account = (payload.account or SMTP_ACCOUNT).strip() or SMTP_ACCOUNT
        password = payload.password if (payload.password or "").strip() else None
        put_smtp_source(
            ctx.store,
            account=account,
            host=payload.host.strip(),
            port=payload.port,
            username=payload.username.strip(),
            password=password,
            sender=payload.sender.strip(),
            use_tls=payload.use_tls,
            enabled=payload.enabled,
        )
        src = resolve_smtp(ctx.store, account=account)
        return _smtp_entry(src) if src else {"account": account, "configured": False}

    @router.delete("/smtp/{account}", tags=["system"])
    def delete_smtp(
        account: str,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Remove one of the user's SMTP accounts. 404 if there's no such account."""
        if not delete_smtp_source(ctx.store, account):
            raise HTTPException(status_code=404, detail=f"No SMTP account '{account}'.")
        return {"account": account, "deleted": True}

    return router
