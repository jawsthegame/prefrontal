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
from prefrontal.modules import enabled_modules
from prefrontal.modules.self_care import (
    apply_self_care_config,
    apply_self_care_mark,
    apply_self_care_reset,
    apply_self_care_unmark,
    self_care_status,
)
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
    DASHBOARD_HTML,
    GUIDE_HTML,
    HOUSEHOLD_HTML,
    PROJECTS_HTML,
    REVIEW_HTML,
    SETTINGS_HTML,
    STATS_HTML,
    lens_html,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    ApnsTokenRegistration,
    GuideProgress,
    SelfCareConfig,
    SelfCareMark,
    SmtpConfig,
)
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
