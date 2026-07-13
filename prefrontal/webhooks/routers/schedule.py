"""HTTP routes tagged "schedule".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

import json
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
from prefrontal.clock import TS_FMT, local_datetime, local_day_bounds
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
    DEFAULT_TRAVEL_PAD_AUTOLEARN,
    DEFAULT_TRAVEL_PAD_FRACTION,
    TRAVEL_PAD_AUTOLEARN_KEY,
    TRAVEL_PAD_FRACTION_KEY,
    _attend_slugs,
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
from prefrontal.geo import DEFAULT_HOME_RADIUS_M
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
from prefrontal.memory.patterns import (
    departure_late_stats,
)
from prefrontal.memory.store import (
    feed_label,
)
from prefrontal.modules.registry import (
    is_enabled as module_enabled,
)
from prefrontal.modules.registry import (
    is_muted as module_muted,
)
from prefrontal.packs.registry import get as get_pack
from prefrontal.packs.registry import is_enabled as pack_enabled
from prefrontal.panic import (
    build_panic,
    evaluate_panic_check,
    render_panic,
)
from prefrontal.reschedule import (
    STATUS_DRAFTED,
    STATUS_FORWARDED,
    RescheduleResult,
    build_reschedule_draft,
    pick_move_side,
    send_reschedule_draft,
)
from prefrontal.scheduling import (
    DEFAULT_SLOT_DAYS,
    STATE_AVAILABLE_HOURS_KEY,
    WEEKDAYS,
    find_slots,
    window_config_for,
)
from prefrontal.sources import (
    resolve_smtp_for,
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
    AvailableHours,
    CalendarSync,
    CommitmentCreate,
    CommitmentDomain,
    CommitmentHardness,
    CommitmentHidden,
    CommitmentKind,
    CommitmentNotes,
    CommitmentOutcome,
    CommitmentPrepared,
    ConflictDismiss,
    LocationSettings,
    PlaceCreate,
    RescheduleRequest,
    TravelPadding,
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
        (by ``external_id``) and prunes calendar events that disappeared. An event
        that fails validation (bad timestamp, missing field) is skipped — the good
        events still sync, and the response's ``skipped`` / ``skipped_titles`` name
        what was dropped, so one malformed event can't silently kill the feed.
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
                recur_horizon_hours=resolved_settings.calendar_horizon_days * 24.0,
                recur_min_occurrences=resolved_settings.calendar_min_occurrences,
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
            "skipped": summary.skipped,
            "skipped_titles": list(summary.skipped_titles),
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
        # A per-user mute (from the weekly usage nudge) silences the departure
        # nudge here too, not just in the coaching tick — mute is authoritative.
        # (Outcome logging at /departure/record and the read-only /departure/next
        # are deliberately NOT muted: mute silences nudges, not learning or pulls.)
        if module_muted(memory, "time_blindness"):
            return {
                "fire": False,
                "message": None,
                "reminder": None,
                "location_known": False,
                "skipped": "module_muted",
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
        limit: int = 50,
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

        ``limit`` (1–1000, default 50) bounds the upcoming list. The dashboard card
        and widget use the default; the calendar page passes a high limit so it can
        show everything we have (the card windows to the next week client-side).
        """
        memory = ctx.store
        limit = max(1, min(1000, limit))
        return {
            "commitments": memory.upcoming_commitments(limit=limit),
            "previous": memory.previous_commitments(),
            "hidden": memory.hidden_commitments(),
            "calendars": resolved_settings.calendar_label_map,
            # Feed slugs treated as "work" (attend-mode). The dashboard uses these
            # to show the post-mortem "felt prepared?" prompt only on work items.
            "attend_calendars": list(_attend_slugs(memory)),
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
        # policy the todo suggester uses. Per-weekday "available hours" (when set)
        # supersede it day-by-day and mark whole days unavailable.
        cfg = window_config_for(resolved_settings, memory)
        found = find_slots(
            memory.upcoming_commitments(),
            utcnow(),
            tz,
            minutes=float(minutes),
            days=days,
            awake_band=cfg.awake_band(),
            band_for_weekday=cfg.band_for_weekday,
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

    def _padding_payload(store: Any) -> dict[str, Any]:
        """The pad as a whole-number percentage plus how it was set.

        ``source`` is the coaching-state provenance: ``"inferred"`` (learned from
        the departure late-rate by the learning pass), ``"explicit"`` (hand-set on
        this page), or ``None`` (never written). ``learned`` is the convenient
        boolean the card uses to show the auto-set note; ``reason`` is the plain
        one-liner explaining *how* a learned value was reached (the departure
        headcount behind it); ``autolearn`` is the master switch's state.
        """
        fraction = store.get_float(TRAVEL_PAD_FRACTION_KEY, DEFAULT_TRAVEL_PAD_FRACTION)
        source = (store.all_state().get(TRAVEL_PAD_FRACTION_KEY, {}) or {}).get("source")
        reason: str | None = None
        if source == "inferred":
            stats = departure_late_stats(store.episodes_by_type("departure", limit=200))
            if stats is not None:
                late, total = stats
                pct = round(100 * late / total)
                reason = (
                    "Prefrontal set this automatically from your departures — "
                    f"you left late on {late} of the last {total} ({pct}%)."
                )
        return {
            "percent": round(max(fraction, 0.0) * 100),
            "source": source,
            "learned": source == "inferred",
            "reason": reason,
            "autolearn": store.get_bool(TRAVEL_PAD_AUTOLEARN_KEY, DEFAULT_TRAVEL_PAD_AUTOLEARN),
        }

    @router.get("/departure/padding", tags=["schedule"])
    def get_departure_padding(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The travel-time safety padding, as a whole-number percentage.

        Reads the ``travel_pad_fraction`` coaching key (a fraction) and returns it
        as a percentage for the Settings control — ``0.15`` → ``15``. ``0`` means
        the padding is off. Also reports whether the value was **learned** from the
        user's departure lateness (``source='inferred'``, with a ``reason`` string
        explaining how) or hand-set here (``'explicit'``), and the ``autolearn``
        master-switch state — so the card can label it, explain it, and offer both a
        reset and an off switch.
        """
        return _padding_payload(ctx.store)

    @router.post("/departure/padding", tags=["schedule"])
    def set_departure_padding(
        payload: TravelPadding,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set the travel-time padding from Settings, or hand it back to the learner.

        Three mutually-exclusive shapes, in precedence order:

        - ``autolearn`` present → flip the master switch only (``on``/``off``); when
          off, the learning pass stops auto-populating the pad. ``percent``/``auto``
          are ignored.
        - ``auto=true`` → re-mark the stored value ``inferred``, clearing a manual
          override so the learner resumes tuning it from the departure late-rate.
        - otherwise → store ``percent`` as the ``travel_pad_fraction`` fraction
          (percent ÷ 100) as an **explicit** user choice the learner leaves alone.

        ``0`` turns padding off; ``percent`` is clamped to 0–100 by the schema, and
        padding only ever lengthens the estimate.
        """
        memory = ctx.store
        if payload.autolearn is not None:
            memory.set_state(
                TRAVEL_PAD_AUTOLEARN_KEY,
                "on" if payload.autolearn else "off",
                source="explicit",
            )
            return _padding_payload(memory)
        if payload.auto:
            # Keep the current number but drop the "explicit" flag, so the learner's
            # override guard no longer skips it on the next pass.
            current = memory.get_float(TRAVEL_PAD_FRACTION_KEY, DEFAULT_TRAVEL_PAD_FRACTION)
            memory.set_state(
                TRAVEL_PAD_FRACTION_KEY, f"{max(current, 0.0):g}", source="inferred"
            )
            return _padding_payload(memory)
        if payload.percent is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="percent is required unless auto is set",
            )
        fraction = payload.percent / 100.0
        memory.set_state(TRAVEL_PAD_FRACTION_KEY, f"{fraction:g}", source="explicit")
        return _padding_payload(memory)

    def _stored_available_hours(store: Any) -> dict[str, Any]:
        """The raw stored per-weekday schedule dict (``{}`` when unconfigured)."""
        raw = (store.all_state().get(STATE_AVAILABLE_HOURS_KEY, {}) or {}).get("value")
        if isinstance(raw, str):
            try:
                loaded = json.loads(raw)
            except (ValueError, TypeError):
                return {}
            if isinstance(loaded, dict):
                return loaded
        return {}

    def _available_hours_payload(store: Any) -> dict[str, Any]:
        """The full seven-day schedule for the Settings control.

        Always returns all seven weekdays so a client can render the whole week:
        a configured day echoes its stored ``available``/``start``/``end`` (kept
        even while unavailable, so toggling a day back on restores its band); an
        unconfigured day falls back to the user's flat waking band (the off-zone
        complement). ``configured`` tells the client whether these are the user's
        explicit hours or the inherited default.
        """
        default_start, default_end = window_config_for(resolved_settings, store).awake_band()
        stored = _stored_available_hours(store)
        days: dict[str, dict[str, Any]] = {}
        for name in WEEKDAYS:
            entry = stored.get(name)
            if isinstance(entry, dict):
                days[name] = {
                    "available": bool(entry.get("available", True)),
                    "start": str(entry.get("start") or default_start),
                    "end": str(entry.get("end") or default_end),
                }
            else:
                days[name] = {
                    "available": True, "start": default_start, "end": default_end,
                }
        return {"configured": bool(stored), "days": days}

    @router.get("/schedule/available-hours", tags=["schedule"])
    def get_available_hours(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The per-weekday "available hours" the slot-finder works within.

        Returns all seven weekdays (`mon`…`sun`), each with `available` and a local
        `start`/`end` `HH:MM` band. When the user hasn't set them, every day falls
        back to their flat waking band (the off-zone complement) and `configured`
        is `false`. These hours gate `/calendar/slots` and the assistant's
        "find me a time": an unavailable day yields no slots, and an available day
        is searched only inside its band.
        """
        return _available_hours_payload(ctx.store)

    @router.post("/schedule/available-hours", tags=["schedule"])
    def set_available_hours(
        payload: AvailableHours,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set the per-weekday available hours from Settings.

        A **partial** write: only the weekdays present in `days` are updated; the
        rest keep their stored value (so a client can save one day at a time). Each
        day's `start`/`end` are validated `HH:MM` and, when `available`, `end` must
        be after `start` (schema-enforced). Stored as the `available_hours` coaching
        key, marked an explicit user choice, and the fresh seven-day view is echoed
        back so the client re-renders from the response.
        """
        memory = ctx.store
        stored = _stored_available_hours(memory)
        for name, day in payload.days.items():
            stored[name] = {
                "available": day.available, "start": day.start, "end": day.end,
            }
        memory.set_state(STATE_AVAILABLE_HOURS_KEY, json.dumps(stored), source="explicit")
        return _available_hours_payload(memory)

    # -- Location settings (tunables on web; the phone keeps only the opt-in) --
    #
    # Stored as individual coaching-state keys (not a JSON blob) so the existing
    # `home_radius_m` key — already read by outing gating and trip detection —
    # stays authoritative and shared, per the issue's "consolidate where sensible".
    STATE_GEOFENCE_RADIUS_KEY = "geofence_radius_m"
    STATE_LOCATION_POST_INTERVAL_KEY = "location_post_interval_s"
    STATE_VISITS_ENABLED_KEY = "location_visits_enabled"

    def _location_settings_payload(store: Any) -> dict[str, Any]:
        """The four location tunables, with defaults applied for unset keys."""
        return {
            "home_radius_m": store.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M),
            "geofence_radius_m": store.get_float(STATE_GEOFENCE_RADIUS_KEY, 120.0),
            "post_interval_s": int(store.get_float(STATE_LOCATION_POST_INTERVAL_KEY, 300)),
            "visits_enabled": store.get_bool(STATE_VISITS_ENABLED_KEY, True),
        }

    @router.get("/schedule/location-settings", tags=["schedule"])
    def get_location_settings(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The native app's location tunables (radii, feed cadence, `CLVisit` on/off).

        The web-configurable knobs the iOS app reads on refresh and applies to its
        CoreLocation monitoring. The **master opt-in** is deliberately absent — it
        must live on the phone (only it can trigger the OS "Always Allow" prompt).
        `home_radius_m` is the same key outing return-gating and trip detection
        already read, surfaced here so all the location tunables sit in one place.
        """
        return _location_settings_payload(ctx.store)

    @router.post("/schedule/location-settings", tags=["schedule"])
    def set_location_settings(
        payload: LocationSettings,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set one or more location tunables from the web dashboard.

        A **partial** write: only the fields present in the body are updated (the
        rest keep their stored value), each stored as an explicit user choice so a
        learner never overwrites it. Bounds are schema-enforced (422 on out-of-range).
        Echoes the fresh full settings back so the client re-renders from the response.
        """
        memory = ctx.store
        provided = payload.model_dump(exclude_unset=True)
        if "home_radius_m" in provided:
            memory.set_state("home_radius_m", f"{provided['home_radius_m']:g}", source="explicit")
        if "geofence_radius_m" in provided:
            memory.set_state(
                STATE_GEOFENCE_RADIUS_KEY, f"{provided['geofence_radius_m']:g}", source="explicit"
            )
        if "post_interval_s" in provided:
            memory.set_state(
                STATE_LOCATION_POST_INTERVAL_KEY,
                str(int(provided["post_interval_s"])),
                source="explicit",
            )
        if "visits_enabled" in provided:
            memory.set_state(
                STATE_VISITS_ENABLED_KEY,
                "1" if provided["visits_enabled"] else "0",
                source="explicit",
            )
        return _location_settings_payload(memory)

    @router.get("/care/sheet", tags=["schedule"])
    def care_sheet(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Caregiver surface data — upcoming ``care`` appointments + care-admin todos.

        The read behind the read-only ``/care`` lens (the caregiver counterpart to
        the household ``/kids`` sheet). Gated on the **Caregiver context pack**
        (``PREFRONTAL_PACKS=caregiver``), *not* household membership — a solo
        caregiver for an aging parent isn't a co-parent. When the pack is off,
        ``enabled`` is ``false`` and the lists are empty so the page can prompt to
        turn it on.

        - ``appointments``: upcoming commitments classified ``kind='care'`` — an
          aging parent's / ill partner's appointment the caregiver arranges or
          attends (the care-recipient counterpart to a kid's ``child`` appointment).
        - ``todos``: open todos in the pack's caregiver categories (``medical`` /
          ``admin`` / ``caregiving``) — the paperwork-and-errands load the surface
          exists to make legible.
        """
        memory = ctx.store
        if not pack_enabled("caregiver", resolved_settings):
            return {"enabled": False, "appointments": [], "todos": []}
        # Enabled implies registered, so get_pack never raises here.
        care_categories = set(get_pack("caregiver").categories)
        appointments = [
            {
                "id": c["id"],
                "title": c["title"],
                "start_at": c.get("start_at"),
                "end_at": c.get("end_at"),
                "location": c.get("location"),
                "calendar": c.get("calendar"),
            }
            for c in memory.upcoming_commitments(limit=100)
            if c.get("kind") == "care"  # commitments.KIND_CARE
        ]
        todos = [
            {
                "id": t["id"],
                "title": t["title"],
                "category": t.get("category"),
                "deadline": t.get("deadline"),
                "priority": t.get("priority"),
            }
            for t in memory.open_todos()
            if t.get("category") in care_categories
        ]
        return {"enabled": True, "appointments": appointments, "todos": todos}

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
            pad_fraction=dep["pad_fraction"],
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

        Both lists exclude anything the user has dismissed, and **every** pair —
        firm double-booking or soft possible — carries a ``key`` to dismiss it via
        ``POST /commitments/conflicts/dismiss``. ``conflicts`` are firm
        double-bookings (two real events overlap); ``possible_conflicts`` are soft
        — a placeholder (Busy/Block/Hold) overlapping a real event.
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
                # A dismissal key on every pair, so a real double-booking is
                # dismissable too (not just soft possibles).
                "key": conflict_dismissal_key(c),
            }

        return {
            "conflicts": [pair(c) for c in hard],
            "possible_conflicts": [pair(c) for c in possible],
        }

    @router.post("/commitments/conflicts/dismiss", tags=["schedule"])
    def dismiss_possible_conflict(
        payload: ConflictDismiss,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Dismiss a conflict by its ``key`` (from the conflicts list).

        Works for either a firm double-booking or a soft possible conflict. The
        key is the event *pair's* identity, so the dismissal stays put across
        re-syncs and reschedules: it keeps holding as long as those two events
        still overlap. A brand-new overlap involving a *different* event has a
        different key, so it still surfaces.
        """
        memory = ctx.store
        memory.dismiss_conflict(payload.key)
        return {"dismissed": payload.key}

    @router.post("/commitments/conflicts/reschedule", tags=["schedule"])
    def reschedule_conflict(
        payload: RescheduleRequest,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Resolve a double-booking by asking one side to move (delegation-style).

        Identifies the conflicting pair by its ``key`` (from
        ``GET /commitments/conflicts``), picks which appointment to move (the
        softer/later one by default; override with ``move: "a"|"b"``), and drafts a
        short, polite reschedule request to the other party — offering a few of your
        open slots as alternatives. The local model writes the draft when reachable,
        with an honest offline fallback.

        Confirm-first by design: with ``send`` omitted/false this only **previews**
        the draft (``status: "drafted"``, nothing leaves the box). With ``send:
        true`` and a ``to`` recipient it emails the notice over your own SMTP source
        (``resolve_smtp_for``) — ``forwarded`` on success (and the conflict is then
        dismissed so it stops re-alerting), or ``failed`` with the draft kept for
        manual sending if SMTP isn't configured or the relay errors (nothing lost).
        """
        memory = ctx.store
        # Recompute the current conflicts and find the pair by its dismissal key —
        # the same identity the conflicts list and dismiss endpoint use. Reschedule
        # applies only to *firm* double-bookings (two real events): a soft
        # "possible" conflict is a placeholder/hold overlapping a real event, which
        # has no genuine other party to email, so it's rejected explicitly below
        # rather than silently drafted against a "Busy" block.
        hard, possible = partition_conflicts(
            find_conflicts(memory.upcoming_commitments()), memory.dismissed_conflicts()
        )
        match = next(
            (c for c in hard if conflict_dismissal_key(c) == payload.key), None
        )
        if match is None:
            if any(conflict_dismissal_key(c) == payload.key for c in possible):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "that overlap is a soft possible-conflict (a placeholder/hold "
                        "block), not a firm double-booking; reschedule applies only to "
                        "firm double-bookings"
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no such active conflict (it may have been resolved or dismissed)",
            )

        # Which side moves: an explicit override, else the suggested (softer) one.
        if payload.move == "a":
            keep, move = match.b, match.a
        elif payload.move == "b":
            keep, move = match.a, match.b
        else:
            keep, move = pick_move_side(match.a, match.b)

        tz = resolved_settings.timezone

        def _local(ts: str | None) -> Any:
            return local_datetime(_parse_dt_or_none(ts), tz)

        move_start_local = _local(move["start_at"])
        move_when = (
            move_start_local.strftime("%a %b %-d, %-I:%M %p")
            if move_start_local
            else None
        )

        # Offer a few open slots the same length as the moved appointment as
        # alternatives (read-only; honors the user's available-hours band).
        slot_labels: list[str] = []
        if payload.offer_slots:
            span = _parse_dt_or_none(move.get("end_at"))
            start = _parse_dt_or_none(move.get("start_at"))
            minutes = 30.0
            if span is not None and start is not None and span > start:
                minutes = max(15.0, (span - start).total_seconds() / 60.0)
            cfg = window_config_for(resolved_settings, memory)
            for w in find_slots(
                memory.upcoming_commitments(), utcnow(), tz,
                minutes=minutes, days=DEFAULT_SLOT_DAYS,
                awake_band=cfg.awake_band(), band_for_weekday=cfg.band_for_weekday,
                limit=3,
            ):
                start_local, end_local = _local(w.start), _local(w.end)
                if start_local is None or end_local is None:
                    continue
                slot_labels.append(
                    f"{start_local.strftime('%a %b %-d')}, "
                    f"{start_local.strftime('%-I:%M %p')}–{end_local.strftime('%-I:%M %p')}"
                )

        client = ollama_client if ollama_client.available() else None
        draft = build_reschedule_draft(
            move_title=move["title"],
            move_when=move_when,
            recipient_name=payload.recipient_name,
            owner_name=ctx.user.get("display_name") or None,
            note=payload.note,
            slots=slot_labels or None,
            client=client,
        )

        if not payload.send:
            result = RescheduleResult(
                STATUS_DRAFTED, draft.subject, draft.body, recipient=payload.to,
                detail="preview only — nothing sent", offline=draft.offline,
            )
        else:
            smtp = resolve_smtp_for(memory, domain=move.get("domain"))
            result = send_reschedule_draft(draft, recipient=payload.to, smtp=smtp)

        dismissed: str | None = None
        if result.status == STATUS_FORWARDED:
            # Sent — stop this pair from re-alerting while we await a reply.
            memory.dismiss_conflict(payload.key)
            dismissed = payload.key

        def side(x: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": x["id"],
                "title": x["title"],
                "start_at": x["start_at"],
                "calendar": feed_label(x.get("external_id")),
            }

        return {
            "moved": side(move),
            "kept": side(keep),
            "status": result.status,
            "subject": result.subject,
            "body": result.body,
            "recipient": result.recipient,
            "detail": result.detail,
            "offline": result.offline,
            "slots": slot_labels,
            "dismissed": dismissed,
        }

    @router.post("/commitments/{commitment_id}/kind", tags=["schedule"])
    def set_commitment_kind(
        commitment_id: int,
        payload: CommitmentKind,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Correct a commitment's kind (``self``, ``child``, ``care``, or ``fyi``).

        Covers every miscategorization: your own commitment mistaken for FYI (or
        vice versa), and — driven from the household sheet's "Upcoming
        appointments" — an event wrongly tagged as a kid's, reclassified to
        ``self`` so it drops off the shared sheet but stays your own commitment.

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

    @router.post("/commitments/{commitment_id}/prepared", tags=["schedule"])
    def set_commitment_prepared(
        commitment_id: int,
        payload: CommitmentPrepared,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Record "did you feel prepared?" for a past work commitment (``yes``/``no``).

        A reflection independent of the made/missed outcome — a work commitment
        can be attended yet felt under-prepared. Passing ``prepared: null`` clears
        it. Like the outcome, it's a user field kept across calendar re-syncs.
        Returns the updated row.
        """
        prepared = payload.prepared
        if prepared is not None:
            prepared = prepared.strip().lower()
            if prepared not in ("yes", "no"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="prepared must be 'yes', 'no', or null",
                )
        memory = ctx.store
        if memory.get_commitment(commitment_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="commitment not found"
            )
        updated = memory.set_commitment_prepared(commitment_id, prepared)
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
