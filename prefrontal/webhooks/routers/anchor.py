"""HTTP routes tagged "anchor".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    DEFAULT_ABANDON_RATIO,
    DEFAULT_HOME_RADIUS_M,
    LEVELS,
    Annotated,
    Any,
    Depends,
    HTMLResponse,
    HTTPException,
    MemoryStore,
    OutingReturn,
    OutingStart,
    OutingStarted,
    Query,
    Request,
    ScopedRequest,
    Settings,
    _delivery_fields,
    _dismiss_page,
    _dismiss_url,
    _outing_return_confirmation,
    _outing_started_confirmation,
    analyze_impact,
    at_risk,
    build_message,
    escalation_level,
    haversine_m,
    impact_phrase,
    infer_time_window,
    is_abandoned,
    is_at_home,
    level_rank,
    module_enabled,
    parse_time_window,
    project_free_time,
    record_outing_abandoned,
    record_outing_return,
    resolve_user,
    status,
    verify_dismiss,
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
    """Build the "anchor" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post(
        "/webhooks/outing/start",
        response_model=OutingStarted,
        status_code=status.HTTP_201_CREATED,
        tags=["anchor"],
    )
    def outing_start(
        payload: OutingStart,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> OutingStarted:
        """Declare an outing: a stated intention and a time window.

        The window is resolved in order of confidence: ``time_window_minutes`` if
        given ('explicit'), else parsed from the text like "back in 15 minutes"
        ('parsed'), else **inferred** from the intention — the local model first,
        then a keyword heuristic, then a default ('llm'/'heuristic'/'default').
        Only a blank intention (nothing to reason from) responds 422.
        """
        memory = ctx.store
        window = payload.time_window_minutes
        source = "explicit"
        if window is None:
            parsed = parse_time_window(payload.intention)
            if parsed is not None:
                window, source = parsed, "parsed"
        if window is None:
            inferred = infer_time_window(payload.intention, client=ollama_client)
            if inferred is not None:
                window, source = inferred
        if window is None or window <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Could not determine a time window: the intention is empty. "
                    "Send a non-empty intention, or include 'time_window_minutes'."
                ),
            )
        outing_id = memory.start_outing(
            payload.intention,
            window,
            home_lat=payload.home_lat,
            home_lon=payload.home_lon,
        )
        return OutingStarted(
            outing_id=outing_id,
            intention=payload.intention,
            time_window_minutes=window,
            time_window_source=source,
            confirmation=_outing_started_confirmation(payload.intention, window, source),
        )

    @router.post("/webhooks/outing/check", tags=["anchor"])
    async def outing_check(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Evaluate active outings and report which nudges are due.

        n8n polls this on a schedule. For each active outing:

        - If the body carries ``current_lat``/``current_lon`` and the user is
          within the home radius, the outing is **passively closed** as returned
          (location confirms the return) and no nudge fires — so a forgotten
          "I'm back" tap, or coming home early, never triggers a call.
        - Otherwise, if elapsed time has blown past the abandon ratio, the outing
          is **auto-closed** as abandoned so it stops lingering active.
        - Otherwise the elapsed-time escalation level is computed; when a *new*
          level is crossed since the last poll it sets ``fire=true``, records the
          level so it fires once, and includes the ``message``.

        For an active outing, **impact analysis** runs: the realistic free-time
        is projected from the stated window and the learned time bias, and any
        upcoming commitments that would be at risk are listed (and named in the
        nudge message). Each returned item reports its post-check ``status``
        (``active``/``returned``/``abandoned``); n8n acts on ``fire == true``.
        """
        memory = ctx.store
        # Location-Aware Task Anchor is the module that owns outing escalation.
        # If it's disabled, this proactive nudge never fires (the module's
        # interventions are off, not just its profile section).
        if not module_enabled("location_anchor", resolved_settings):
            return {"active": [], "skipped": "module_disabled"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cur_lat = body.get("current_lat")
        cur_lon = body.get("current_lon")
        # Fall back to the phone's last-known position (POSTed to
        # /webhooks/location) when the poll body carries no explicit coordinates
        # — so location-gating works without a Home Assistant feed.
        if cur_lat is None or cur_lon is None:
            last = memory.get_location()
            if last is not None:
                cur_lat, cur_lon = last["lat"], last["lon"]
        name = ctx.user.get("display_name") or ""
        handle = ctx.user.get("handle") or ""
        settings: Settings = request.app.state.settings
        home_radius = memory.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M)
        abandon_ratio = memory.get_float("abandon_after_ratio", DEFAULT_ABANDON_RATIO)
        bias = memory.get_float("time_estimation_bias", 1.0)
        commitments = memory.upcoming_commitments()
        delivery = _delivery_fields(memory)

        results: list[dict[str, Any]] = []
        for outing in memory.active_outings():
            elapsed = outing["elapsed_minutes"] or 0.0
            window = outing["time_window_minutes"]

            distance_m = None
            if (
                cur_lat is not None
                and cur_lon is not None
                and outing["home_lat"] is not None
                and outing["home_lon"] is not None
            ):
                distance_m = round(
                    haversine_m(outing["home_lat"], outing["home_lon"], cur_lat, cur_lon)
                )
            at_home = is_at_home(distance_m, home_radius) if distance_m is not None else None

            level, fire, message, outcome = "none", False, "", None
            impacts: list[dict[str, Any]] = []
            if at_home:
                # Location confirms the user is home — passive return.
                closed = memory.close_outing(outing["id"], status="returned")
                outcome = record_outing_return(memory, closed)["outcome"]
                outing_status = "returned"
            elif is_abandoned(elapsed, window, abandon_ratio):
                closed = memory.close_outing(outing["id"], status="abandoned")
                outcome = record_outing_abandoned(memory, closed)["outcome"]
                outing_status = "abandoned"
            else:
                level = escalation_level(elapsed, window)
                fire = level_rank(level) > level_rank(outing["last_level"])
                # Impact analysis: project realistic free-time from the bias and
                # see which upcoming commitments are now at risk.
                risky = []
                if commitments:
                    projected = project_free_time(outing["departure_at"], window, bias)
                    risky = at_risk(analyze_impact(projected, commitments))
                    impacts = [
                        {
                            "commitment_id": i.commitment["id"],
                            "title": i.commitment["title"],
                            "start_at": i.commitment["start_at"],
                            "slack_minutes": i.slack_minutes,
                            "hardness": i.commitment.get("hardness"),
                        }
                        for i in risky
                    ]
                if fire:
                    memory.set_outing_level(outing["id"], level)
                    message = build_message(
                        level, elapsed_minutes=elapsed, window_minutes=window, name=name
                    ) + impact_phrase(risky)
                    memory.record_nudge(kind="outing", message=message, level=level)
                outing_status = "active"

            results.append(
                {
                    "outing_id": outing["id"],
                    "intention": outing["intention"],
                    "elapsed_minutes": round(elapsed, 1),
                    "time_window_minutes": window,
                    "level": level,
                    "fire": fire,
                    "message": message,
                    "distance_m": distance_m,
                    "at_home": at_home,
                    "status": outing_status,
                    "outcome": outcome,
                    "impact": impacts,
                    "hard_conflict": any(i["hardness"] == "hard" for i in impacts),
                    # One-tap link that silences further escalation for this
                    # outing (empty unless a public origin + signing key exist).
                    "dismiss_url": _dismiss_url(settings, handle, "outing", outing["id"]),
                    **delivery,
                }
            )
        return {"active": results}

    @router.post("/webhooks/outing/return", tags=["anchor"])
    def outing_return(
        payload: OutingReturn,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Close an outing and log intention-vs-actual for pattern tracking.

        Records the actual time out as a ``task`` episode (predicted = stated
        window, actual = minutes out, outcome = ``success`` if within the window
        else ``miss``) so the time-blindness learning can use it later.
        """
        memory = ctx.store
        outing_id = payload.outing_id
        if outing_id is None:
            recent = memory.most_recent_active_outing()
            if recent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active outing to return.",
                )
            outing_id = recent["id"]

        closed = memory.close_outing(outing_id, status=payload.status)
        if closed is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outing {outing_id} is not active.",
            )

        if payload.status == "returned":
            recorded = record_outing_return(memory, closed)
        else:
            recorded = record_outing_abandoned(memory, closed)
        actual = closed.get("actual_minutes")
        actual = round(actual, 1) if actual is not None else None
        window = closed.get("time_window_minutes")
        return {
            "outing_id": outing_id,
            "status": payload.status,
            "actual_minutes": actual,
            "time_window_minutes": window,
            "outcome": recorded["outcome"],
            "episode_id": recorded["episode_id"],
            "confirmation": _outing_return_confirmation(
                payload.status, actual, window, recorded["outcome"]
            ),
        }

    @router.get("/nudge/dismiss", response_class=HTMLResponse, tags=["anchor"])
    def nudge_dismiss(request: Request, t: str = "") -> str:
        """Silence a nudge from a one-tap link in its Pushover notification.

        The link is opened from the phone as a bare browser GET that carries no
        ``X-Prefrontal-Token`` header, so it authenticates from the signed ``t``
        token alone — which embeds the user handle and the target. Idempotent:
        re-tapping a spent link still renders the confirmation page.

        - ``outing`` — pin the escalation at its ceiling so no further soft/firm/
          call nudge fires, **without** closing the outing (the user is still
          out; tapping "I'm back" later still closes it normally).
        - ``departure`` — record the commitment as dismissed so
          ``/webhooks/departure/check`` stops nudging for it (a future
          occurrence is a new commitment id and re-arms on its own).
        """
        settings: Settings = request.app.state.settings
        parsed = verify_dismiss(t, settings.session_secret)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This dismiss link is invalid or has expired.",
            )
        handle, kind, target_id = parsed
        root: MemoryStore = request.app.state.store
        user = root.get_user(handle)
        if user is None or user["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user."
            )
        memory = root.scoped(user["id"])

        if kind == "outing":
            outing = memory.get_outing(target_id)
            if outing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="No such outing."
                )
            memory.set_outing_level(target_id, LEVELS[-1])
            label = outing.get("intention") or "your outing"
            headline = f"Nudges silenced for “{label}.”"
        else:  # "departure"
            memory.dismiss_departure(target_id)
            headline = "Departure reminder dismissed."

        return _dismiss_page(headline)

    @router.get("/outings", tags=["anchor"])
    def outings_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read-only snapshot of outings for monitoring — **no side effects**.

        Unlike ``/webhooks/outing/check`` (which fires nudges and auto-closes
        abandoned outings), this never mutates state, so a dashboard can poll it
        freely. Active outings carry their current elapsed-time escalation
        ``level`` (computed, not recorded); recent outings are returned newest
        first for history.
        """
        memory = ctx.store
        active = [
            {
                "outing_id": o["id"],
                "intention": o["intention"],
                "elapsed_minutes": round(o.get("elapsed_minutes") or 0.0, 1),
                "time_window_minutes": o["time_window_minutes"],
                "level": escalation_level(
                    o.get("elapsed_minutes") or 0.0, o["time_window_minutes"]
                ),
                "departure_at": o["departure_at"],
            }
            for o in memory.active_outings()
        ]
        recent = [
            {
                "outing_id": o["id"],
                "intention": o["intention"],
                "status": o["status"],
                "time_window_minutes": o["time_window_minutes"],
                "departure_at": o["departure_at"],
                "returned_at": o.get("returned_at"),
            }
            for o in memory.recent_outings(limit=20)
        ]
        return {"active": active, "recent": recent}

    @router.get("/nudges", tags=["anchor"])
    def nudges_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        limit: Annotated[
            int, Query(ge=1, le=50, description="Max nudges to return (newest first).")
        ] = 5,
    ) -> dict[str, Any]:
        """Recently sent nudges, newest first — **no side effects**.

        A read-only "what did Prefrontal last tell me?" feed of the escalation
        and departure nudges the system decided to send (recorded when they fire
        in ``/webhooks/outing/check`` and ``/webhooks/departure/check``). The
        widget and dashboard poll this so a missed push notification is still
        visible at a glance.
        """
        return {"nudges": ctx.store.recent_nudges(limit=limit)}

    # -- Impulsivity (capture-and-defer) -------------------------------------

    return router
