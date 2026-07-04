"""HTTP routes tagged "anchor".

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
    Query,
    Request,
    status,
)
from fastapi.responses import (
    HTMLResponse,
)

from prefrontal.clock import TS_FMT
from prefrontal.coaching import (
    resolve_ack,
)
from prefrontal.config import (
    Settings,
)
from prefrontal.departure import (
    departure_kwargs,
    travel_leads,
)
from prefrontal.focus_balance import (
    FOCUS_DOMAINS,
    normalize_focus_domain,
)
from prefrontal.household import (
    CHECKIN_ACTION_RESPONSE,
    award_stars_and_notify,
    checkin_summary,
    week_key,
)
from prefrontal.impact import (
    utcnow,
)
from prefrontal.memory.store import (
    MemoryStore,
)
from prefrontal.modules.hyperfocus import (
    record_focus_end,
    record_focus_switched,
)
from prefrontal.modules.location_anchor import (
    DEFAULT_ABANDON_RATIO,
    DEFAULT_HOME_RADIUS_M,
    LEVELS,
    apply_outing_evaluation,
    escalation_level,
    evaluate_outing,
    infer_time_window,
    parse_time_window,
    record_outing_abandoned,
    record_outing_return,
)
from prefrontal.modules.registry import (
    is_enabled as module_enabled,
)
from prefrontal.modules.self_care import SELF_CARE_ACTIONS, apply_self_care_action
from prefrontal.panic import (
    resolve_panic_step,
)
from prefrontal.scheduling import local_datetime
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.helpers import (
    _delivery_fields,
    _dismiss_page,
    _dismiss_url,
    _nudge_actions,
    _outing_return_confirmation,
    _outing_started_confirmation,
)
from prefrontal.webhooks.oauth import (
    verify_action,
    verify_dismiss,
)
from prefrontal.webhooks.schemas import (
    OutingDomain,
    OutingReturn,
    OutingStart,
    OutingStarted,
)
from prefrontal.webhooks.services import RouterServices

#: One-tap ``/nudge/act`` action → the coaching ``context_key`` its target lives
#: under, so a tap can resolve the delivered nudge's channel outcome (spec §8).
#: Actions absent here (the ``switch_*`` pause resolutions) aren't coaching cues,
#: so they map to no context and :func:`resolve_ack` no-ops.
_ACT_CONTEXT = {
    "focus_end": "focus",
    "outing_return": "outing",
    "outing_abandon": "outing",
    "made_it": "departure",
    "missed_it": "departure",
    # One-tap domain filing from the trip-label ask (all resolve the "trip" cue).
    **{f"trip_domain_{d}": "trip" for d in FOCUS_DOMAINS},
}


def build_router(services: RouterServices) -> APIRouter:
    """Build the "anchor" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    ollama_client = services.ollama

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
            domain=normalize_focus_domain(payload.domain),
        )
        return OutingStarted(
            outing_id=outing_id,
            intention=payload.intention,
            time_window_minutes=window,
            time_window_source=source,
            confirmation=_outing_started_confirmation(payload.intention, window, source),
        )

    @router.post("/webhooks/outing/domain", tags=["anchor"])
    def outing_domain(
        payload: OutingDomain,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """(Re)file a declared outing into a life-domain for the focus-balance rollup.

        Mirrors ``/webhooks/trip/domain`` on the passive side — set a sphere on an
        outing you declared without one, or correct a misfiled one. A null/blank
        ``domain`` clears it.
        """
        updated = ctx.store.set_outing_domain(
            payload.outing_id, normalize_focus_domain(payload.domain)
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No outing with id {payload.outing_id}.",
            )
        return {"outing_id": updated["id"], "domain": updated["domain"]}

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
        is projected from the stated window and the learned time bias, then
        *cascaded* through the schedule so knock-on commitments (not just the
        first collision) are flagged — each ``impact`` item carries
        ``delay_minutes``/``projected_start``/``caused_by`` and the nudge names the
        domino chain. Each returned item reports its post-check ``status``
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
        # Real travel legs from the phone's current location, so "you're behind"
        # reflects the drive to each venue, not a flat lead. Empty (→ static leads)
        # when location or destination coords are missing.
        dep = departure_kwargs(memory)
        leads = travel_leads(
            commitments, cur_lat, cur_lon,
            bias=dep["bias"], speed_kmh=dep["speed_kmh"],
            road_factor=dep["road_factor"], prep_minutes=dep["prep_minutes"],
        )
        delivery = _delivery_fields(memory)

        results: list[dict[str, Any]] = []
        for outing in memory.active_outings():
            # Shared decision + side effects, identical to the coaching agent's
            # LocationAnchorModule.evaluate (one source of truth — spec §12 step 2).
            ev = evaluate_outing(
                outing,
                cur_lat=cur_lat,
                cur_lon=cur_lon,
                home_radius=home_radius,
                abandon_ratio=abandon_ratio,
                bias=bias,
                commitments=commitments,
                name=name,
                lead_override=leads,
            )
            outing_status, outcome = apply_outing_evaluation(memory, outing, ev)

            results.append(
                {
                    "outing_id": outing["id"],
                    "intention": outing["intention"],
                    "elapsed_minutes": round(outing["elapsed_minutes"] or 0.0, 1),
                    "time_window_minutes": outing["time_window_minutes"],
                    "level": ev.level,
                    "fire": ev.fire,
                    "message": ev.message,
                    "distance_m": ev.distance_m,
                    "at_home": ev.at_home,
                    "status": outing_status,
                    "outcome": outcome,
                    "impact": ev.impacts,
                    "hard_conflict": any(i["hardness"] == "hard" for i in ev.impacts),
                    # One-tap link that silences further escalation for this
                    # outing (empty unless a public origin + signing key exist).
                    "dismiss_url": _dismiss_url(settings, handle, "outing", outing["id"]),
                    # One-tap ntfy buttons: I'm back / Abandon (empty if unconfigured).
                    "actions": _nudge_actions(settings, handle, "outing", outing["id"]),
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

    @router.get("/nudge/act", response_class=HTMLResponse, tags=["anchor"])
    def nudge_act(request: Request, t: str = "") -> str:
        """Act on a nudge from a one-tap ntfy button — no app switch, no header.

        The button is an ntfy ``http`` action firing a background GET whose signed
        ``t`` embeds the user handle and target, so it authenticates without an
        ``X-Prefrontal-Token`` header (like ``/nudge/dismiss``). Each action runs
        the same close/record logic as its full endpoint:

        - ``focus_end`` — end an active focus session (Wrap up).
        - ``outing_return`` / ``outing_abandon`` — close an outing (I'm back / Abandon).
        - ``made_it`` / ``missed_it`` — log a commitment's departure outcome.
        - ``panic_step_done`` — resolve an overwhelm first-step nudge as done.
        - ``meal_ate`` / ``meal_snooze`` / ``water_drank`` / ``water_snooze`` —
          confirm/snooze a self-care basic-needs check.

        Idempotent: re-tapping a spent button (session/outing already closed) still
        renders a friendly confirmation rather than erroring.
        """
        settings: Settings = request.app.state.settings
        parsed = verify_action(t, settings.session_secret)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This action link is invalid or has expired.",
            )
        handle, action, target_id = parsed
        root: MemoryStore = request.app.state.store
        user = root.get_user(handle)
        if user is None or user["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user."
            )
        memory = root.scoped(user["id"])

        # A tap is an acknowledgement: if the coaching agent delivered this nudge,
        # record which channel landed (feeds channel_response; spec §8). A no-op
        # for nudges the engine didn't originate.
        resolve_ack(memory, _ACT_CONTEXT.get(action, ""), target_id)

        if action == "focus_end":
            closed = memory.close_focus_session(target_id, status="ended")
            if closed is None:
                return _dismiss_page("That focus session is already wrapped up.")
            record_focus_end(memory, closed, outcome=None, breadcrumb=None)
            label = closed.get("intention") or closed.get("task") or "your session"
            return _dismiss_page(f"Wrapped up “{label}.” Nice work.")

        if action in ("outing_return", "outing_abandon"):
            status_ = "returned" if action == "outing_return" else "abandoned"
            closed = memory.close_outing(target_id, status=status_)
            if closed is None:
                return _dismiss_page("That outing is already closed.")
            if status_ == "returned":
                record_outing_return(memory, closed)
                return _dismiss_page("Welcome back — outing logged.")
            record_outing_abandoned(memory, closed)
            return _dismiss_page("Outing closed. No worries.")

        if action.startswith("trip_domain_"):
            # One-tap file a just-finished trip into a life-sphere (focus balance).
            # target_id is the trip id; the domain is the action suffix.
            domain = action[len("trip_domain_") :]
            if domain not in FOCUS_DOMAINS:
                return _dismiss_page("That trip filing option is no longer available.")
            updated = memory.set_trip_domain(target_id, domain)
            if updated is None:
                return _dismiss_page("That trip is no longer available.")
            return _dismiss_page(f"Filed under {domain}. Thanks — that keeps your balance honest.")

        if action in ("switch_return", "switch_defer", "switch_switch"):
            # Resolve a reflective pause in one tap (mirrors /webhooks/focus/resolve;
            # the impulse was already counted when the pause fired). The buttons
            # ride on the SwitchPause response, so the session is the pull's target.
            session = memory.get_focus_session(target_id)
            if session is None or session.get("status") != "active":
                return _dismiss_page("That focus block is already resolved.")
            task = session.get("intended_task") or "your task"
            if action == "switch_return":
                return _dismiss_page(f"Staying on “{task}.” 👊")
            if action == "switch_defer":
                memory.add_todo(
                    f"Come back to what pulled you off “{task}”", source="impulse"
                )
                memory.mark_switch_deferred(target_id)
                return _dismiss_page(
                    "Parked it as a todo — back to what you were doing. "
                    "(Rename it later if you like.)"
                )
            closed = memory.close_focus_session(target_id, status="switched")
            if closed is not None:
                record_focus_switched(memory, closed)
            return _dismiss_page(f"Switched away from “{task}.” Logged it.")

        if action == "panic_step_done":
            # target_id is the pending panic episode logged when the nudge fired.
            done = resolve_panic_step(memory, target_id)
            if not done:
                return _dismiss_page("Already logged — nice work either way.")
            return _dismiss_page("Logged — you took the first step. 👏 That's the hard part.")

        if action in SELF_CARE_ACTIONS:
            # A self-care check has no entity id — it acts on "now"/"today", so we
            # recompute the local date at tap time rather than trust the synthetic
            # target. apply_self_care_action owns the confirm/snooze semantics.
            now = utcnow()
            today = local_datetime(now, settings.timezone).strftime("%Y-%m-%d")
            headline = apply_self_care_action(memory, action, now=now, today=today)
            return _dismiss_page(headline or "Done.")

        if action == "star_award":
            # target_id is the star-chart agreement id. Award one star, attributed
            # to whoever tapped; a crossed reward goal congratulates both parents.
            result = award_stars_and_notify(
                memory, target_id, delta=1, awarded_by=user["id"],
                note="via prompt", settings=settings,
            )
            if result is None:
                return _dismiss_page("That star chart is no longer available.")
            who = result["child_name"] or "the kids"
            line = f"⭐ Star added for {who} — {result['total']} total."
            if result["goals_reached"]:
                line += " 🎉 Reward unlocked!"
            return _dismiss_page(line)

        if action == "star_skip":
            return _dismiss_page("No star today — that's okay. 🙂")

        if action in CHECKIN_ACTION_RESPONSE:
            # Weekly mental-load check-in: record how *this* parent's week felt
            # (no entity id — resolve the ISO week at tap time). Once everyone has
            # replied, send both parents the gentle shared note.
            from prefrontal.integrations.delivery import (
                deliver_to_household,
                household_notice,
            )

            now_local = local_datetime(utcnow(), settings.timezone)
            week = week_key(now_local)
            memory.record_checkin_response(
                week=week, user_id=user["id"], response=CHECKIN_ACTION_RESPONSE[action]
            )
            hid = memory.household_id_or_none()
            members = [
                m for m in memory.household_members(hid)
                if m.get("status") in (None, "active")
            ] if hid is not None else []
            responses = memory.checkin_responses(week) if hid is not None else []
            if members and len(responses) >= len(members):
                note = checkin_summary([r["response"] for r in responses])
                deliver_to_household(
                    memory, hid, household_notice(note, channel="push"), settings=settings
                )
            return _dismiss_page("Thanks for checking in 💛 That's really helpful.")

        if action == "digest_seen":
            # Mark the shared sheet seen "now" so the delta digest won't re-surface
            # these changes — the one-tap equivalent of opening /kids.
            now = utcnow()
            memory.set_state(
                "household_seen_at", now.strftime(TS_FMT), source="inferred"
            )
            return _dismiss_page("All caught up 💛")

        if action == "chore_done":
            # target_id is the chore id. Log it done for today (local date),
            # attributed to whoever tapped — so a partner picking up a slipped
            # chore closes the loop and stops the miss-handoff re-firing.
            today = local_datetime(utcnow(), settings.timezone).strftime("%Y-%m-%d")
            result = memory.log_chore_done(
                chore_id=target_id, done_on=today, done_by=user["id"]
            )
            if result is None:
                return _dismiss_page("That chore is no longer on the list.")
            return _dismiss_page(f"Done — “{result['title']}” is sorted for today. 🙌")

        # made_it / missed_it — log a commitment's departure outcome.
        commitment = memory.get_commitment(target_id)
        title = (commitment or {}).get("title") or "your commitment"
        made = action == "made_it"
        memory.log_episode(
            "departure",
            acknowledged=True,
            context=f"commitment: {title}",
            outcome="success" if made else "miss",
            notes=f"one-tap: {'made it' if made else 'missed it'}",
        )
        memory.dismiss_departure(target_id)
        return _dismiss_page(
            f"Logged — you made it to “{title}.” 🎯" if made
            else f"Logged — ran late for “{title}.” It happens."
        )

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
