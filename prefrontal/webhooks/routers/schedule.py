"""HTTP routes tagged "schedule".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from datetime import (
    timedelta,
)
from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)

from prefrontal.briefing import (
    build_briefing,
    record_briefing_feedback,
    render_briefing,
)
from prefrontal.classify import (
    classify_kind,
)
from prefrontal.clock import TS_FMT
from prefrontal.clock import (
    parse_ts as _parse_dt_or_none,
)
from prefrontal.coaching import (
    in_quiet_hours,
)
from prefrontal.commitments import (
    HARDNESS,
    KINDS,
    OUTCOMES,
    conflict_dismissal_key,
    find_conflicts,
    is_attendable,
    normalize_event,
    partition_conflicts,
    sync_calendar,
)
from prefrontal.config import (
    Settings,
)
from prefrontal.departure import (
    DEFAULT_DEPARTURE_GRACE_MINUTES,
    attribute_departure,
    departure_kwargs,
    evaluate_departure_check,
    plan_departure,
    plan_upcoming_departures,
    record_departure_outcome,
    travel_leads,
)
from prefrontal.encouragement import OPEN_DAY_CHOICES, OPEN_DAY_KEY
from prefrontal.focus_balance import normalize_focus_domain
from prefrontal.geocode import (
    normalize_query,
)
from prefrontal.impact import (
    cascade_at_risk,
    cascade_impact,
    cascade_phrase,
    project_free_time,
    utcnow,
)
from prefrontal.memory.store import (
    feed_label,
)
from prefrontal.modules.registry import (
    is_enabled as module_enabled,
)
from prefrontal.panic import (
    build_panic,
    evaluate_panic_check,
    render_panic,
)
from prefrontal.scheduling import (
    DEFAULT_SLOT_DAYS,
    find_slots,
    local_datetime,
    local_day_bounds,
    window_config_for,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.helpers import (
    _dismiss_url,
    _nudge_actions,
)
from prefrontal.webhooks.notify import (
    act_url,
    panic_actions,
)
from prefrontal.webhooks.schemas import (
    CalendarSync,
    CommitmentCreate,
    CommitmentDomain,
    CommitmentHardness,
    CommitmentHidden,
    CommitmentKind,
    CommitmentNotes,
    CommitmentOutcome,
    ConflictDismiss,
    PlaceCreate,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "schedule" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    ollama_client = services.ollama
    _run_geocode = services.run_geocode

    @router.post("/webhooks/calendar/sync", tags=["schedule"])
    def calendar_sync(
        payload: CalendarSync,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Sync a batch of upcoming calendar events into ``commitments``.

        n8n posts the current window of events on a schedule; this upserts them
        (by ``external_id``) and prunes calendar events that disappeared. A bad
        timestamp rejects the whole batch with 422 (never partially applies).
        """
        memory = ctx.store
        # Consult Ollama only when reachable — one liveness check up front avoids a
        # slow per-event timeout storm when it's down (new events then default to
        # 'self', the conservative, conflict-preserving choice). The roster pass is
        # deterministic and offline, so a kid's appointment still reaches the shared
        # sheet as 'child' even with Ollama down; we build the classifier whenever
        # either signal is available.
        child_names = memory.child_names()
        llm = ollama_client if ollama_client.available() else None
        examples = memory.kind_feedback_examples() if llm is not None else None
        classify = None
        if llm is not None or child_names:

            def classify(title: str) -> tuple[str, str]:
                return classify_kind(
                    title, client=llm, examples=examples, child_names=child_names
                )

        try:
            summary = sync_calendar(
                memory,
                [e.model_dump() for e in payload.events],
                classify=classify,
                default_tz=resolved_settings.timezone,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        # Fill in destination coordinates for events that arrived with only a
        # free-text location, so the departure reminder can estimate travel time.
        # Curated places + the cache are offline; the network geocoder is used
        # only when geocoding_enabled is on. Best-effort: never blocks the sync.
        geocoded = _run_geocode(memory)
        return {
            "added": summary.added,
            "updated": summary.updated,
            "cancelled": summary.cancelled,
            "upcoming": summary.upcoming,
            "conflicts": summary.conflicts,
            "new_conflict": summary.new_conflict,
            "possible_conflicts": summary.possible_conflicts,
            "new_possible_conflict": summary.new_possible_conflict,
            "geocoded": geocoded["resolved"],
        }

    @router.post("/webhooks/departure/check", tags=["schedule"])
    async def departure_check(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Evaluate upcoming commitments and report whether to nudge a departure.

        n8n polls this on a schedule (the Departure Reminder workflow). For each
        upcoming commitment it computes a leave-by time: from the phone's
        last-known location and the commitment's ``dest_lat``/``dest_lon`` when
        both are known (a local, bias-adjusted travel estimate), otherwise from
        the commitment's static ``lead_minutes``. The most urgent reminder-worthy
        commitment is returned; ``fire`` is ``true`` only when its
        ``(commitment, level)`` pair is new since the last poll, so a standing
        reminder doesn't re-alert every cycle. Current coordinates may be
        overridden in the request body (``current_lat``/``current_lon``).
        """
        memory = ctx.store
        # Departure timing is a Time Blindness intervention; if that module is
        # off, the proactive departure nudge never fires.
        if not module_enabled("time_blindness", resolved_settings):
            return {
                "fire": False,
                "message": None,
                "reminder": None,
                "location_known": False,
                "skipped": "module_disabled",
            }
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        name = ctx.user.get("display_name") or ""
        handle = ctx.user.get("handle") or ""
        settings: Settings = request.app.state.settings

        # The decision (plan → pick → fire/debounce → record) lives in the domain
        # layer so it's unit-testable and a CLI twin can share it; the router adds
        # the HTTP-only one-tap decoration below.
        result = evaluate_departure_check(
            memory,
            name=name,
            current_lat=body.get("current_lat"),
            current_lon=body.get("current_lon"),
        )
        reminder = result.reminder
        if reminder is not None:
            cid = reminder["commitment_id"]
            reminder = {
                **reminder,
                # One-tap link that dismisses this commitment's departure nudges.
                "dismiss_url": _dismiss_url(settings, handle, "departure", cid),
                # One-tap ntfy buttons: Made it / Missed it (empty if unconfigured).
                "actions": _nudge_actions(settings, handle, "departure", cid),
            }
        return {
            "fire": result.fire,
            "message": result.message,
            "reminder": reminder,
            "location_known": result.location_known,
        }

    @router.post("/webhooks/departure/office-day", tags=["schedule"])
    async def departure_office_day(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Flag (or clear) today as an in-office day.

        Work commitments default to an "attend from here" 5-min reminder (you're
        already where you'll take the meeting). On the mornings you actually commute
        in, tap this once and every work commitment switches back to the
        travel-aware "leave now" nudge for the rest of the day.

        Body (all optional): ``on`` (default ``true``; ``false`` clears the flag)
        and ``hours`` (how long the flag lasts, default 16 — long enough to cover a
        work day, and self-expiring so a forgotten toggle doesn't leak into
        tomorrow). Per-meeting exceptions don't need this: a ``[commute]`` tag in a
        single event's title switches just that one to travel mode.
        """
        memory = ctx.store
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if body.get("on", True) is False:
            memory.set_state("office_day_until", "", source="user")
            return {"office_day": False}

        try:
            hours = float(body.get("hours") or 16.0)
        except (TypeError, ValueError):
            hours = 16.0
        until = (utcnow() + timedelta(hours=hours)).strftime(TS_FMT)
        memory.set_state("office_day_until", until, source="user")
        return {"office_day": True, "until": until}

    @router.post("/webhooks/departure/left", tags=["schedule"])
    async def departure_left(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Record an actual departure and score it against the leave-by time.

        The outcome mirror of ``/webhooks/departure/check``: an iOS "when I leave
        Home" automation (or any client) POSTs when the user heads out, and this
        attributes the departure to the commitment it was for, classifies it
        on-time vs late against that commitment's computed leave-by, and logs a
        ``departure`` episode — closing the last big gap in outcome capture (the
        learning pass finally sees whether departures land on time).

        Body (all optional): ``departed_at`` (``YYYY-MM-DD HH:MM:SS`` / ISO;
        defaults to now), ``commitment_id`` (force attribution, skipping the
        matcher), and ``current_lat``/``current_lon`` (else the stored fix) for
        the travel-based leave-by. Idempotent per commitment occurrence, so a
        chatty geofence that fires twice logs once.
        """
        memory = ctx.store
        # Departure timing is a Time Blindness intervention; honor the toggle.
        if not module_enabled("time_blindness", resolved_settings):
            return {"recorded": False, "skipped": "module_disabled"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        # Accept the stored "YYYY-MM-DD HH:MM:SS" form or ISO-8601 (what iOS emits,
        # e.g. "2026-07-02T09:48:00Z"): swap the T separator; the parser's [:19]
        # slice drops any trailing Z/offset. Defaults to now for a live geofence.
        raw_departed = body.get("departed_at")
        if isinstance(raw_departed, str):
            raw_departed = raw_departed.strip().replace("T", " ", 1)
        departed_at = _parse_dt_or_none(raw_departed) or utcnow()

        cur_lat = body.get("current_lat")
        cur_lon = body.get("current_lon")
        if cur_lat is None or cur_lon is None:
            last = memory.get_location()
            if last is not None:
                cur_lat, cur_lon = last["lat"], last["lon"]

        plans = [
            plan_departure(
                c,
                current_lat=cur_lat,
                current_lon=cur_lon,
                now=departed_at,
                **departure_kwargs(memory),
            )
            for c in memory.upcoming_commitments()
        ]

        forced = body.get("commitment_id")
        if forced is not None:
            plan = next((p for p in plans if p.commitment["id"] == forced), None)
        else:
            plan = attribute_departure(plans, departed_at)

        if plan is None:
            return {"recorded": False, "reason": "no_matching_commitment"}

        # Idempotent per commitment occurrence: a geofence can fire twice.
        signature = f"{plan.commitment['id']}:{plan.commitment['start_at']}"
        if memory.get_state("last_departure_outcome", "") == signature:
            return {
                "recorded": False,
                "reason": "already_recorded",
                "commitment_id": plan.commitment["id"],
            }

        grace = memory.get_float(
            "departure_grace_minutes", DEFAULT_DEPARTURE_GRACE_MINUTES
        )
        recorded = record_departure_outcome(
            memory, plan, departed_at, grace_minutes=grace, tz=resolved_settings.timezone
        )
        memory.set_state("last_departure_outcome", signature, source="inferred")
        # They've left — stop any further departure nudges for this commitment.
        memory.dismiss_departure(plan.commitment["id"])

        title = plan.commitment.get("title") or "your commitment"
        lateness = recorded["lateness_minutes"]
        if recorded["outcome"] == "miss":
            confirmation = (
                f"Logged — you left for “{title}” about {round(lateness)} min late."
            )
        else:
            spare = round(max(0.0, -lateness))
            confirmation = (
                f"Logged — you left for “{title}” on time"
                + (f" (~{spare} min to spare)." if spare >= 1 else ".")
                + " Nice."
            )
        return {
            "recorded": True,
            "confirmation": confirmation,
            "title": title,
            "leave_by": plan.leave_by,
            "departed_at": departed_at.strftime(TS_FMT),
            **recorded,
        }

    @router.post(
        "/commitments", status_code=status.HTTP_201_CREATED, tags=["schedule"]
    )
    def commitment_create(
        payload: CommitmentCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add a single commitment manually (source ``manual``).

        If a ``location`` is given without explicit coordinates, a geocode pass
        runs (curated places + cache always; network geocoder only when
        ``geocoding_enabled`` is on) so the departure reminder can use travel time.
        """
        memory = ctx.store
        try:
            fields = normalize_event(
                {**payload.model_dump(), "source": "manual"},
                default_tz=resolved_settings.timezone,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        commitment_id, _ = memory.upsert_commitment(**fields)
        if fields.get("dest_lat") is None and fields.get("location"):
            _run_geocode(memory)
        commitment = memory.get_commitment(commitment_id)
        return {
            "commitment_id": commitment_id,
            "dest_lat": commitment.get("dest_lat") if commitment else None,
            "dest_lon": commitment.get("dest_lon") if commitment else None,
        }

    @router.post("/commitments/geocode", tags=["schedule"])
    def commitments_geocode(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Run a geocoding pass over commitments missing coordinates.

        Useful for backfilling after enabling geocoding or adding curated places.
        Curated places + the cache are always consulted; the network geocoder is
        used only when ``geocoding_enabled`` is on.
        """
        memory = ctx.store
        return _run_geocode(memory)

    @router.post("/places", status_code=status.HTTP_201_CREATED, tags=["schedule"])
    def place_create(
        payload: PlaceCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add (or update) a curated place alias used before any geocoding.

        The ``name`` is normalized to a match key; re-posting the same name
        updates its coordinates. Matched against a commitment's location and
        title, so "gym" resolves "Gym session" instantly and offline.
        """
        memory = ctx.store
        name = normalize_query(payload.name)
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Place 'name' is empty after normalization.",
            )
        place_id = memory.add_place(
            name, payload.lat, payload.lon, label=payload.label or payload.name
        )
        return {"place_id": place_id, "name": name}

    @router.get("/places", tags=["schedule"])
    def places_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """List curated place aliases (most specific name first)."""
        memory = ctx.store
        return {"places": memory.places()}

    @router.get("/commitments", tags=["schedule"])
    def commitments_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """List active upcoming commitments, soonest first.

        Each commitment carries a ``calendar`` label and a ``calendar_key`` (the
        feed slug). The response also echoes the operator-configured ``calendars``
        label map so the dashboard can render a friendly, colored pill per
        calendar without hard-coding any feed names or colors.

        ``commitments`` excludes hidden ones (so the widget and every other reader
        drops them); the separate ``hidden`` list carries them for the dashboard's
        un-hide affordance. ``previous`` carries recently-elapsed commitments still
        awaiting a made/missed answer (surfaced for about a day) so the dashboard
        can offer the "I made it / missed it" affordance.
        """
        memory = ctx.store
        return {
            "commitments": memory.upcoming_commitments(),
            "previous": memory.previous_commitments(),
            "hidden": memory.hidden_commitments(),
            "calendars": resolved_settings.calendar_label_map,
        }

    @router.get("/calendar/slots", tags=["schedule"])
    def calendar_slots(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        minutes: int = 30,
        days: int = DEFAULT_SLOT_DAYS,
    ) -> dict[str, Any]:
        """Find open slots of a specific length over the next few days — read-only.

        "Where does a 45-minute block fit this week?" For each upcoming local day
        it scans the waking band (the user's off-zone complement, so a slot is
        never offered overnight), subtracts the calendar's commitments, and returns
        every remaining gap at least ``minutes`` long, soonest first. Today's band
        starts no earlier than now, so every slot is one you can still use. Purely
        derived and side-effect-free — it never nudges or records, so a widget or
        the read-only calendar page can poll it freely.

        Query params: ``minutes`` (the slot length to look for, 1–1440; default 30)
        and ``days`` (how many local days ahead to scan, 1–14; default 7). Each slot
        carries UTC ``start_at``/``end_at`` plus pre-formatted local ``day`` /
        ``start`` / ``end`` labels so a client renders without re-deriving the zone.
        """
        if not 1 <= minutes <= 1440:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="minutes must be between 1 and 1440",
            )
        if not 1 <= days <= 14:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="days must be between 1 and 14",
            )
        memory = ctx.store
        tz = resolved_settings.timezone
        # The waking band is the user's own off-zone complement (state over env over
        # the 06:00–22:00 default), so slot-finding respects the same "not overnight"
        # policy the todo suggester uses.
        awake = window_config_for(resolved_settings, memory).awake_band()
        found = find_slots(
            memory.upcoming_commitments(),
            utcnow(),
            tz,
            minutes=float(minutes),
            days=days,
            awake_band=awake,
        )

        def dump(w: Any) -> dict[str, Any]:
            start_local = local_datetime(_parse_dt_or_none(w.start), tz)
            end_local = local_datetime(_parse_dt_or_none(w.end), tz)
            return {
                "start_at": w.start,
                "end_at": w.end,
                "minutes": w.minutes,
                "day": start_local.strftime("%a %b %-d"),
                "start": start_local.strftime("%-I:%M %p"),
                "end": end_local.strftime("%-I:%M %p"),
            }

        return {"minutes": minutes, "days": days, "slots": [dump(w) for w in found]}

    @router.get("/departure/next", tags=["schedule"])
    def departure_next(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Leave-by time for the soonest upcoming commitment — read-only.

        The passive, side-effect-free companion to
        ``POST /webhooks/departure/check``: that endpoint debounces and *records* a
        nudge (it drives delivery), so a widget polling it would corrupt the
        fire-once state and expire nudges early. This one only computes the plan for
        the soonest upcoming commitment and returns its ``leave_by`` for display —
        no dedup, no nudge, safe to poll.

        Returns ``{"departure": {...} | None, "location_known": bool}``. The
        ``departure`` mirrors the check endpoint's ``reminder`` fields (minus the
        one-tap HTTP decoration) so a client can show *when to leave* beside the
        commitment's start. ``None`` when nothing is upcoming or the Time Blindness
        module (which owns departure timing) is off.
        """
        # Departure timing is a Time Blindness intervention; when it's off there's
        # no leave-by to surface, matching /webhooks/departure/check.
        if not module_enabled("time_blindness", resolved_settings):
            return {"departure": None, "location_known": False}
        memory = ctx.store
        location_known = memory.get_location() is not None
        plans = plan_upcoming_departures(memory)
        if not plans:
            return {"departure": None, "location_known": location_known}
        top = plans[0]  # soonest first (upcoming_commitments order)
        return {
            "departure": {
                "commitment_id": top.commitment["id"],
                "title": top.commitment["title"],
                "location": top.commitment.get("location"),
                "start_at": top.commitment["start_at"],
                "leave_by": top.leave_by,
                "minutes_until_leave": top.minutes_until_leave,
                "travel_minutes": top.travel_minutes,
                "basis": top.basis,
                "level": top.level,
                "mode": top.mode,
            },
            "location_known": location_known,
        }

    @router.get("/impact/cascade", tags=["schedule"])
    def impact_cascade(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        free_at: str | None = None,
        over_minutes: float | None = None,
        current_lat: float | None = None,
        current_lon: float | None = None,
    ) -> dict[str, Any]:
        """Project a running-late free-time through the day, domino by domino.

        The same cascade that ``/webhooks/outing/check`` runs on an active outing,
        but queryable on its own so the dashboard, briefing, or any client can ask
        "if I'm free at X, what topples?" without an outing in flight.

        The free-time is resolved in priority order: an explicit ``free_at``
        (``YYYY-MM-DD HH:MM:SS`` or ISO), else ``over_minutes`` from now
        (``now + over_minutes``), else the bias-adjusted projection of the newest
        active outing, else now. ``source`` reports which was used.

        The walk is scoped to the seed day and to your **own** commitments:
        being "behind" doesn't carry across a night's sleep (a tight back-to-back
        tomorrow isn't something you're behind on today), and FYI events (where
        someone else will be) never consume your time or topple your schedule —
        the same scoping the panic cascade uses.

        When a location is known (``current_lat``/``current_lon``, else the phone's
        last fix) the per-leg lead times use **real bias-adjusted travel** between
        commitment coordinates instead of each event's static ``lead_minutes`` — so
        a leg you can't actually drive in the flat buffer is flagged. ``travel_aware``
        reports whether any leg got a travel estimate.

        Returns the full chain (schedule order) with each link's
        ``projected_start``/``delay_minutes``/``slack_minutes``/``caused_by``, the
        at-risk subset, a ``hard_conflict`` flag, and a one-line ``phrase``.
        """
        memory = ctx.store

        source = "now"
        free = utcnow()
        if free_at:
            raw = free_at.strip().replace("T", " ", 1)
            parsed = _parse_dt_or_none(raw)
            if parsed is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="free_at must be YYYY-MM-DD HH:MM:SS or ISO-8601",
                )
            free, source = parsed, "explicit"
        elif over_minutes is not None:
            free, source = utcnow() + timedelta(minutes=float(over_minutes)), "over_minutes"
        else:
            outing = memory.most_recent_active_outing()
            if outing is not None:
                bias = memory.get_float("time_estimation_bias", 1.0)
                free = project_free_time(
                    outing["departure_at"], outing["time_window_minutes"], bias
                )
                source = "outing"

        # Scope the walk to the seed's *local* day and to *your own* commitments.
        # Being "behind" is a within-day notion — a domino chain doesn't survive a
        # night's sleep, so a tight back-to-back tomorrow isn't something you're
        # behind on today. The day boundary must be *local*: at 9pm Eastern the
        # UTC day has already rolled over, so a plain ``free.replace(hour=23,…)``
        # UTC window pulls in tomorrow-morning's commitments (see local_day_bounds).
        # ``is_attendable`` then keeps only real, own commitments: FYI events (where
        # someone else will be; never yours to attend) and placeholder/hold blocks
        # ("HOLD", "OOO", "Focus", …; elastic time you'd yield in a pinch) must
        # never be modelled as consuming your time and pushing a real commitment
        # late — the same exclusion the panic cascade and every other surface make.
        _day_start, day_end = local_day_bounds(free, resolved_settings.timezone)
        lower = max(utcnow(), _day_start)
        commitments = [
            c
            for c in memory.commitments_between(
                lower.strftime(TS_FMT), day_end.strftime(TS_FMT)
            )
            if is_attendable(c)
        ]

        def dump(i: Any) -> dict[str, Any]:
            return {
                "commitment_id": i.commitment["id"],
                "title": i.commitment["title"],
                "start_at": i.commitment["start_at"],
                "projected_start": i.projected_start,
                "delay_minutes": i.delay_minutes,
                "slack_minutes": i.slack_minutes,
                "at_risk": i.at_risk,
                "caused_by": i.caused_by,
                "hardness": i.commitment.get("hardness"),
            }

        # Real travel legs (from an explicit location, else the last known fix)
        # replace static leads where coordinates allow; else the cascade falls back.
        if current_lat is None or current_lon is None:
            last = memory.get_location()
            if last is not None:
                current_lat, current_lon = last["lat"], last["lon"]
        dep = departure_kwargs(memory)
        leads = travel_leads(
            commitments, current_lat, current_lon,
            bias=dep["bias"], speed_kmh=dep["speed_kmh"],
            road_factor=dep["road_factor"], prep_minutes=dep["prep_minutes"],
        )

        chain = cascade_impact(free, commitments, lead_override=leads)
        risky = cascade_at_risk(chain)
        return {
            "projected_free_at": free.strftime(TS_FMT),
            "source": source,
            "travel_aware": bool(leads),
            "cascade": [dump(i) for i in chain],
            "at_risk": [dump(i) for i in risky],
            "hard_conflict": any(i.commitment.get("hardness") == "hard" for i in risky),
            "phrase": cascade_phrase(chain).strip(),
        }

    @router.get("/commitments/conflicts", tags=["schedule"])
    def commitments_conflicts(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Report overlaps among upcoming commitments, split by firmness.

        ``conflicts`` are firm double-bookings (two real events overlap).
        ``possible_conflicts`` are soft — a placeholder (Busy/Block/Hold)
        overlapping a real event — excluding any the user has dismissed; each
        carries a ``key`` to dismiss it via ``POST /commitments/conflicts/dismiss``.
        """
        memory = ctx.store
        hard, possible = partition_conflicts(
            find_conflicts(memory.upcoming_commitments()), memory.dismissed_conflicts()
        )

        def side(x: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": x["id"],
                "title": x["title"],
                "start_at": x["start_at"],
                "calendar": feed_label(x.get("external_id")),
            }

        def pair(c: Any) -> dict[str, Any]:
            return {
                "a": side(c.a),
                "b": side(c.b),
                "overlap_minutes": c.overlap_minutes,
            }

        return {
            "conflicts": [pair(c) for c in hard],
            "possible_conflicts": [
                {**pair(c), "key": conflict_dismissal_key(c)} for c in possible
            ],
        }

    @router.post("/commitments/conflicts/dismiss", tags=["schedule"])
    def dismiss_possible_conflict(
        payload: ConflictDismiss,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Dismiss a possible conflict by its ``key`` (from the conflicts list).

        The dismissal sticks across re-syncs but lapses if either event moves or
        is retitled (the key is derived from start time + title).
        """
        memory = ctx.store
        memory.dismiss_conflict(payload.key)
        return {"dismissed": payload.key}

    @router.post("/commitments/{commitment_id}/kind", tags=["schedule"])
    def set_commitment_kind(
        commitment_id: int,
        payload: CommitmentKind,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Correct a commitment's kind (``self`` vs ``fyi``).

        Records the correction as feedback (alongside the model's prior verdict)
        so the classifier's prompt evolves toward the user's judgement, and marks
        the row ``kind_source='user'`` so a later sync never re-classifies it.
        """
        memory = ctx.store
        kind = payload.kind.strip().lower()
        if kind not in KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"kind must be one of {KINDS}",
            )
        current = memory.get_commitment(commitment_id)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_kind(commitment_id, kind, "user")
        # Learn from the correction: store the user's label + what was there
        # before (often the model's verdict) as a few-shot example.
        memory.record_kind_feedback(
            current["title"], kind, llm_kind=current.get("kind")
        )
        return {"commitment": updated}

    @router.post("/commitments/{commitment_id}/hardness", tags=["schedule"])
    def set_commitment_hardness(
        commitment_id: int,
        payload: CommitmentHardness,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set a commitment's hardness (``hard`` vs ``soft``).

        Marks how firm the commitment is — ``hard`` (a must-happen obligation that
        panic/cascade treats as a genuine fire when it slips) vs ``soft`` (an
        elastic block you'd yield in a pinch). Stamps ``hardness_source='user'`` so
        the choice sticks across calendar re-syncs, exactly like a ``kind``
        correction. 422 on an unknown value, 404 if no such commitment.
        """
        memory = ctx.store
        hardness = payload.hardness.strip().lower()
        if hardness not in HARDNESS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"hardness must be one of {HARDNESS}",
            )
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_hardness(commitment_id, hardness)
        return {"commitment": updated}

    @router.post("/commitments/{commitment_id}/hidden", tags=["schedule"])
    def set_commitment_hidden(
        commitment_id: int,
        payload: CommitmentHidden,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Hide (or un-hide) a commitment.

        A hidden commitment is dropped from every surface that reads
        :meth:`~prefrontal.memory.repos.schedule.ScheduleRepo.upcoming_commitments`
        — the dashboard list, the widget, conflict detection, departure reminders
        — while staying in the store so it can be un-hidden and so a calendar
        re-sync doesn't resurrect it. Returns the updated row.
        """
        memory = ctx.store
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_hidden(commitment_id, payload.hidden)
        return {"commitment": updated}

    @router.post("/commitments/{commitment_id}/outcome", tags=["schedule"])
    def set_commitment_outcome(
        commitment_id: int,
        payload: CommitmentOutcome,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Record whether a past commitment was *made* or *missed* (honest self-report).

        Answering resolves the commitment: it drops out of the dashboard's
        recently-elapsed list (which otherwise lingers for about a day). Passing
        ``outcome: null`` clears the answer, surfacing it again if still in-window.
        The outcome is a user judgement kept across calendar re-syncs. Returns the
        updated row.
        """
        outcome = payload.outcome
        if outcome is not None:
            outcome = outcome.strip().lower()
            if outcome not in OUTCOMES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"outcome must be one of {OUTCOMES} or null",
                )
        memory = ctx.store
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_outcome(commitment_id, outcome)
        return {"commitment": updated}

    @router.post("/commitments/{commitment_id}/notes", tags=["schedule"])
    def set_commitment_notes(
        commitment_id: int,
        payload: CommitmentNotes,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set (or clear) a commitment's free-text notes.

        The note is consulted when a nudge is built for this commitment — the
        departure reminder folds it on as a ``Note: …`` hint ("leave now for the
        dentist — Note: bring the insurance card"). ``null``/empty clears it. Like
        the ``hidden``/``outcome`` flags it's a user field kept across calendar
        re-syncs. Returns the updated row (404 if no such commitment).
        """
        memory = ctx.store
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_notes(commitment_id, payload.notes)
        return {"commitment": updated}

    @router.post("/commitments/{commitment_id}/domain", tags=["schedule"])
    def set_commitment_domain(
        commitment_id: int,
        payload: CommitmentDomain,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set (or clear) a commitment's life **domain** (its life-sphere).

        The same axis todos and trips carry — ``work``/``home``/``kids``/… — so a
        kid's appointment reads as ``domain='kids'`` rather than overloading
        ``kind``. The value is snapped onto the canonical vocabulary
        (``child``/``family`` → ``kids``, …) via
        :func:`~prefrontal.focus_balance.normalize_focus_domain`; ``null``/empty
        clears it. Like ``notes``/``hidden``/``outcome`` it's a user field kept
        across calendar re-syncs. Returns the updated row (404 if no such commitment).
        """
        memory = ctx.store
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        domain = normalize_focus_domain(payload.domain)
        updated = memory.set_commitment_domain(commitment_id, domain)
        return {"commitment": updated}

    @router.get("/briefing", tags=["schedule"])
    def briefing(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return today's morning briefing as structured data plus rendered text.

        n8n can deliver the ``text`` directly, or feed it to Ollama for prose
        (or call ``prefrontal summarize``-style). Always fast and model-free here.

        Feedback (👍/👎, which steers the LLM briefing voice via
        :func:`prefrontal.briefing.learned_briefing_guidance`) is offered two ways
        so ``text`` itself stays clean prose: signed one-tap links under
        ``feedback`` for plain-text channels that can only render a URL (n8n / SMS),
        and ``POST /briefing/feedback`` for a token-authed rich client (the
        dashboard renders real buttons against it). Briefing feedback has no entity
        id (it's about "the digest you just read"), so the signed links ride a
        synthetic ``0`` target like the self-care / check-in one-tap actions.
        """
        memory = ctx.store
        settings: Settings = request.app.state.settings
        handle = ctx.user.get("handle") or ""
        helped = act_url(
            settings.oauth_base_url, handle, "briefing_helped", 0,
            settings.session_secret,
        )
        not_helped = act_url(
            settings.oauth_base_url, handle, "briefing_not_helped", 0,
            settings.session_secret,
        )
        b = build_briefing(memory)
        return {
            "date": b.date,
            "format": b.format,
            "today": b.today,
            "conflicts": b.conflicts,
            "slips": b.slips,
            "coaching": b.coaching,
            "spare": b.spare,
            "encouragement": b.encouragement,
            "open_day_choice": memory.get_state(OPEN_DAY_KEY) or None,
            "feedback": {"helped_url": helped, "not_helped_url": not_helped},
            # Clean prose — no footer baked in. A rich client (the dashboard) renders
            # its own buttons via POST /briefing/feedback; plain-text channels use the
            # signed links under "feedback" above.
            "text": render_briefing(b),
        }

    @router.post("/briefing/feedback", tags=["schedule"])
    async def briefing_feedback(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, int]:
        """Record a 👍/👎 vote on today's briefing; returns the running tally.

        Body: ``{"helpful": true | false}``. The dashboard's briefing card posts
        here; the signed ``/nudge/act`` links deliver the same vote from text
        channels. Both land in :func:`prefrontal.briefing.record_briefing_feedback`,
        which steers the LLM briefing voice. ``helpful`` must be a bool — a missing
        or non-bool value is a 422 rather than a silently-miscounted 👎.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        helpful = body.get("helpful") if isinstance(body, dict) else None
        if not isinstance(helpful, bool):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='body must be {"helpful": true|false}',
            )
        return record_briefing_feedback(ctx.store, helpful=helpful)

    @router.post("/briefing/open-day", tags=["schedule"])
    async def briefing_open_day(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Answer the morning brief's open-day choice (spec §6.2).

        On a wide-open day the briefing asks whether you want to take it easy or
        make it count; this records that standing answer so future open days act on
        it instead of re-asking. Body: ``{"choice": "relax" | "accomplish"}`` to
        set it, or ``{"choice": "ask"}`` (or anything else) to clear it and be asked
        again. Only meaningful when the ``encouragement`` layer is on.
        """
        memory = ctx.store
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        choice = str(body.get("choice") or "").strip().lower()
        if choice in OPEN_DAY_CHOICES:
            memory.set_state(OPEN_DAY_KEY, choice, source="user")
            return {"open_day_choice": choice}
        memory.set_state(OPEN_DAY_KEY, "", source="user")
        return {"open_day_choice": None}

    @router.get("/panic", tags=["schedule"])
    def panic(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Overwhelmed-mode triage: what's actually on fire now + one first step.

        Where ``/briefing`` is a calm whole-day overview, this ranks only what is
        bearing down *right now* across calendar, todos, and mail into three
        buckets (``late`` / ``soon`` / ``piling_up``) and returns a single
        concrete first action to break the freeze. Fast and model-free; n8n or a
        Shortcut can deliver the ``text`` directly, or feed it to Ollama for prose.
        """
        memory = ctx.store

        def dump(p: Any) -> dict[str, Any]:
            return {
                "bucket": p.bucket,
                "kind": p.kind,
                "title": p.title,
                "when": p.when,
                "source": p.source,
                "commitment_id": p.commitment_id,
                "todo_id": p.todo_id,
            }

        plan = build_panic(memory)
        return {
            "date": plan.date,
            "counts": plan.counts,
            "first_step": plan.first_step,
            "first_step_for": plan.first_step_for,
            "headline": plan.headline,
            "late": [dump(p) for p in plan.late],
            "soon": [dump(p) for p in plan.soon],
            "piling_up": [dump(p) for p in plan.piling_up],
            "cascade": plan.cascade,
            "text": render_panic(plan),
        }

    @router.post("/webhooks/panic/check", tags=["schedule"])
    def panic_check(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Poll for a *proactive* overwhelm nudge (for n8n / a scheduler).

        Panic mode is normally on-demand, but the worst spikes are the moments you
        don't think to open anything. A poller hits this on a cadence; it fires
        only when the plate genuinely tips into overwhelm (see
        :func:`~prefrontal.panic.overwhelm_level`), and edge-triggers on the level
        — like the departure reminder's signature — so a sustained pile-up nudges
        once, not every poll. A ``panic_alert_cooldown_minutes`` floor keeps it
        quiet for a while after firing even if the level flaps.

        Outside the user's responsive hours (``responsive_hours_start`` /
        ``responsive_hours_end``, the same window the coaching engine gates
        non-critical cues on) the nudge is **deferred, not dropped**: an
        overwhelm spike at 3am holds until the first poll back inside responsive
        hours, then fires. So it can't wake you at night, but it also won't
        silently swallow a genuine pile-up.

        Returns ``{"fire", "level", "message", "first_step", "counts", "headline",
        "actions"}``. Only ``fire == true`` should be delivered as a push/voice
        nudge; when it fires, ``actions`` carries an ntfy ``view`` button that
        opens the full ``/panic`` triage (the dashboard overlay). If a signing key
        and public origin are configured it *also* carries a signed **"✓ Did it"**
        button: tapping it resolves a pending ``panic`` episode to a success, and
        one left unanswered past ``panic_step_ack_window_minutes`` is swept to a
        miss — so the drift pass learns whether the surfaced first step actually
        got done. ``actions`` is empty unless firing and a public origin is set.
        """
        memory = ctx.store
        # Shared decision (edge-trigger + quiet-hours defer + cooldown + sweep +
        # pending-step record) so the endpoint and the native `coach --deliver`
        # tick nudge identically. Ack-ability gates whether a pending step is
        # recorded — a signed one-tap button must be deliverable first.
        ackable = bool(
            resolved_settings.oauth_base_url and resolved_settings.session_secret
        )
        result = evaluate_panic_check(
            memory,
            now=utcnow(),
            quiet_hours=in_quiet_hours(memory, utcnow(), resolved_settings.timezone),
            ackable=ackable,
        )
        actions: list[dict[str, Any]] = []
        if result.fire:
            # A one-tap "Open triage" button so the nudge isn't a dead end, plus a
            # signed "✓ Did it" button when a pending step was recorded (ackable).
            actions = panic_actions(resolved_settings.oauth_base_url)
            if result.step_id is not None:
                actions = (
                    _nudge_actions(
                        resolved_settings,
                        ctx.user.get("handle") or "",
                        "panic",
                        result.step_id,
                    )
                    + actions
                )

        return {
            "fire": result.fire,
            "level": result.level,
            "message": result.message,
            "first_step": result.first_step,
            "counts": result.counts,
            "headline": result.headline,
            "actions": actions,
        }

    # -- Todos (open loops fitted into free time) ----------------------------

    return router
