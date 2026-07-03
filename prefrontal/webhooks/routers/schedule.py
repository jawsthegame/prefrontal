"""HTTP routes tagged "schedule".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.clock import TS_FMT
from prefrontal.departure import departure_kwargs, evaluate_departure_check
from prefrontal.encouragement import OPEN_DAY_CHOICES, OPEN_DAY_KEY
from prefrontal.webhooks._common import (
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    DEFAULT_ALERT_MIN_PRESSING,
    DEFAULT_DEPARTURE_GRACE_MINUTES,
    DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES,
    KINDS,
    Annotated,
    Any,
    CalendarSync,
    CommitmentCreate,
    CommitmentKind,
    ConflictDismiss,
    Depends,
    HTTPException,
    PlaceCreate,
    Request,
    ScopedRequest,
    Settings,
    _dismiss_url,
    _nudge_actions,
    _parse_dt_or_none,
    attribute_departure,
    build_briefing,
    build_panic,
    classify_kind,
    conflict_dismissal_key,
    feed_label,
    find_conflicts,
    in_quiet_hours,
    module_enabled,
    normalize_event,
    normalize_query,
    overwhelm_level,
    panic_actions,
    panic_alert_message,
    partition_conflicts,
    plan_departure,
    record_departure_outcome,
    record_panic_step_sent,
    render_briefing,
    render_panic,
    resolve_user,
    status,
    sweep_pending_panic_steps,
    sync_calendar,
    timedelta,
    utcnow,
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
        # Classify only when Ollama is reachable — one liveness check up front
        # avoids a slow per-event timeout storm when it's down (new events then
        # default to 'self', the conservative, conflict-preserving choice).
        classify = None
        if ollama_client.available():
            examples = memory.kind_feedback_examples()

            def classify(title: str) -> tuple[str, str]:
                return classify_kind(title, client=ollama_client, examples=examples)

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
            memory, plan, departed_at, grace_minutes=grace
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
        """
        memory = ctx.store
        return {
            "commitments": memory.upcoming_commitments(),
            "calendars": resolved_settings.calendar_label_map,
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

    @router.get("/briefing", tags=["schedule"])
    def briefing(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return today's morning briefing as structured data plus rendered text.

        n8n can deliver the ``text`` directly, or feed it to Ollama for prose
        (or call ``prefrontal summarize``-style). Always fast and model-free here.
        """
        memory = ctx.store
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
            "text": render_briefing(b),
        }

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
        plan = build_panic(memory)

        # Sweep any earlier overwhelm first-step nudges the user never answered
        # into a miss, so the learning loop sees the steps that didn't happen too
        # (not just the "Did it" taps). Independent of firing, so it runs every
        # poll — including calm ones and quiet-hours deferrals.
        sweep_pending_panic_steps(
            memory,
            utcnow(),
            window_minutes=memory.get_float(
                "panic_step_ack_window_minutes", DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES
            ),
        )

        try:
            min_pressing = int(
                memory.get_state("panic_alert_min_pressing")
                or DEFAULT_ALERT_MIN_PRESSING
            )
        except (TypeError, ValueError):
            min_pressing = DEFAULT_ALERT_MIN_PRESSING
        cooldown = memory.get_float(
            "panic_alert_cooldown_minutes", DEFAULT_ALERT_COOLDOWN_MINUTES
        )

        level = overwhelm_level(plan, min_pressing=min_pressing)
        prev = memory.get_state("last_panic_level", "calm")
        fire = level == "overwhelmed" and prev != "overwhelmed"

        # Quiet-hours gate: an overwhelm spike outside responsive hours is
        # *deferred*, not dropped. Return without advancing last_panic_level, so
        # the edge is preserved and the first poll back in responsive hours still
        # fires — otherwise a 3am pile-up would silently consume its own edge and
        # never nudge. (Unlike the cooldown below, which intentionally consumes
        # the edge so a sustained pile-up nudges once, not every poll.)
        if fire and in_quiet_hours(memory, utcnow(), resolved_settings.timezone):
            return {
                "fire": False,
                "level": level,
                "message": "",
                "first_step": plan.first_step,
                "counts": plan.counts,
                "headline": plan.headline,
                "actions": [],
            }

        # Cooldown floor: even on a fresh edge, stay quiet if we alerted recently.
        if fire:
            last_at = _parse_dt_or_none(memory.get_state("last_panic_alert_at"))
            if last_at is not None:
                elapsed = (utcnow() - last_at).total_seconds() / 60.0
                if elapsed < cooldown:
                    fire = False

        memory.set_state("last_panic_level", level, source="inferred")
        message = ""
        actions: list[dict[str, Any]] = []
        if fire:
            name = (memory.get_state("user_name") or "").strip() or None
            message = panic_alert_message(plan, name=name)
            # A one-tap "Open triage" button so the nudge is not a dead end: it
            # carries the first step inline, and this opens the full picture.
            actions = panic_actions(resolved_settings.oauth_base_url)
            # When we can deliver a *signed* one-tap button, log a pending "panic"
            # episode and prepend a "Did it" button, so the drift pass learns
            # whether the surfaced first step actually got done. Gate on
            # ack-ability (public origin + signing key): an undeliverable button
            # must not leave a phantom episode the sweep would count as a miss.
            ackable = bool(
                resolved_settings.oauth_base_url and resolved_settings.session_secret
            )
            if plan.first_step and ackable:
                step_id = record_panic_step_sent(memory, plan, now=utcnow())
                actions = (
                    _nudge_actions(
                        resolved_settings, ctx.user.get("handle") or "", "panic", step_id
                    )
                    + actions
                )
            memory.set_state(
                "last_panic_alert_at",
                utcnow().strftime(TS_FMT),
                source="inferred",
            )

        return {
            "fire": fire,
            "level": level,
            "message": message,
            "first_step": plan.first_step,
            "counts": plan.counts,
            "headline": plan.headline,
            "actions": actions,
        }

    # -- Todos (open loops fitted into free time) ----------------------------

    return router
