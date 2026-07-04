"""HTTP routes tagged "ingestion".

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
    status,
)

from prefrontal.focus_balance import (
    FOCUS_DOMAINS,
    balance_summary_line,
    build_focus_balance,
    normalize_focus_domain,
)
from prefrontal.mail import (
    ingest_messages,
)
from prefrontal.mail.feedback import (
    learned_corrections,
    learned_denylist,
)
from prefrontal.modules.registry import (
    is_enabled as module_enabled,
)
from prefrontal.triage import (
    apply as triage_apply,
)
from prefrontal.triage import (
    classify as triage_classify,
)
from prefrontal.triage import (
    signal_from_payload,
)
from prefrontal.trips import (
    TRIP_CATEGORIES,
    apply_reflection,
    normalize_trip_category,
    process_location,
)
from prefrontal.webhooks._common import (
    ACTION_OUTCOME,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    EpisodeCreated,
    HomeSet,
    LocationPing,
    MailSync,
    ShortcutPayload,
    TriageForget,
    TriageIn,
    TripDomain,
    TripLabel,
    TripReflect,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "ingestion" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    n8n = services.n8n
    ollama_client = services.ollama
    # Reasoning-heavy paths are per-agent selectable (Claude vs local): mail
    # triage and the sensor that reads a trip reflection. The snappy outcome
    # inference on a reflection stays on the local model.
    triage_client = services.provider.client("triage")
    sensor_client = services.provider.client("sensor")

    def _run_triage(signal, store) -> dict[str, Any]:
        """Classify + apply a signal, honoring the triage settings; return a report."""
        client = triage_client if resolved_settings.triage_use_llm else None
        decision = triage_classify(
            signal, client=client, drop_threshold=resolved_settings.triage_drop_threshold
        )
        report = triage_apply(signal, decision, store, n8n=n8n, client=client)
        return {
            "handled": True,
            "decision": {
                "kind": decision.kind, "urgency": decision.urgency, "route": decision.route,
                "reason": decision.reason, "confidence": decision.confidence,
                "decided_by": decision.source, "fields": decision.fields,
            },
            **report,
        }

    @router.post(
        "/webhooks/shortcut",
        response_model=EpisodeCreated,
        status_code=status.HTTP_201_CREATED,
        tags=["ingestion"],
    )
    def shortcut(
        payload: ShortcutPayload,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> EpisodeCreated:
        """Log a one-tap outcome from an iOS Shortcut.

        Maps ``made_it``/``missed_it``/``partial`` to the corresponding episode
        ``outcome``; ``log`` uses the explicit ``outcome`` field. Best-effort
        fires an ``episode.logged`` event to n8n (a no-op unless configured).
        """
        memory = ctx.store

        if payload.action == "log":
            if not payload.outcome:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="action='log' requires an explicit 'outcome'.",
                )
            outcome = payload.outcome
        else:
            outcome = ACTION_OUTCOME[payload.action]

        # Sensible default for acknowledgement when the Shortcut doesn't say:
        # a one-tap report means the user engaged, so success/partial imply ack.
        acknowledged = payload.acknowledged
        if acknowledged is None and payload.action in ("made_it", "partial"):
            acknowledged = True

        episode_id = memory.log_episode(
            payload.episode_type,
            predicted_value=payload.predicted_value,
            actual_value=payload.actual_value,
            acknowledged=acknowledged,
            channel=payload.channel,
            context=payload.context,
            outcome=outcome,
            notes=payload.notes,
        )

        result = n8n.trigger(
            "episode.logged",
            {"episode_id": episode_id, "episode_type": payload.episode_type, "outcome": outcome},
        )
        return EpisodeCreated(
            episode_id=episode_id, outcome=outcome, n8n_delivered=result.delivered
        )

    @router.post("/webhooks/n8n", tags=["ingestion"])
    async def n8n_inbound(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Receive an event pushed by an n8n workflow and triage it.

        The body is normalized into a :class:`~prefrontal.triage.Signal`, then
        **classified and routed** (:func:`prefrontal.triage.classify` /
        ``apply``): a dated event becomes a commitment, an actionable one an
        augmented todo, an outcome an episode, noise a logged drop — and an
        ``urgency == "now"`` signal also fires a ``triage.urgent`` n8n event.
        Idempotent on ``(source, external_id)``. Returns the decision + routing.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {"value": body}
        signal = signal_from_payload(body, source=str(body.get("source") or "n8n"))
        return _run_triage(signal, ctx.store)

    @router.post("/triage", tags=["ingestion"])
    def triage_signal(
        payload: TriageIn,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Classify + route a single normalized signal posted directly.

        The source-agnostic entry point (an iOS Shortcut or an Apps Script mail
        digest can post a :class:`TriageIn` without going through n8n). Same
        classify+apply as ``/webhooks/n8n``; returns the decision and what it
        routed to.
        """
        signal = signal_from_payload(payload.model_dump(), source=payload.source)
        return _run_triage(signal, ctx.store)

    @router.get("/triage/recent", tags=["ingestion"])
    def triage_recent(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        limit: int = 50,
    ) -> dict[str, Any]:
        """Recent triage decisions (newest first) with their reasons — for the
        dashboard and for explainability ("why was this dropped?"). Read-only."""
        return {"decisions": ctx.store.recent_triage(max(1, min(limit, 200)))}

    @router.post("/webhooks/location", tags=["ingestion"])
    def location_ping(
        payload: LocationPing,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Record the phone's current position (from an iOS Shortcut).

        This is the single source of "where am I now" for the rest of the
        system: ``/webhooks/outing/check`` reads it to gate the coffee-shop
        nudge without a Home Assistant feed, and ``/webhooks/departure/check``
        reads it to estimate travel time to upcoming commitments. A location
        automation ("when I leave Home", on a schedule, or one-tap) can POST it.

        When the ``trip_tracking`` module is enabled and a home coordinate is set
        (``POST /webhooks/home``), each ping is also folded into the closed-loop
        **trip** state machine (:func:`prefrontal.trips.process_location`): leaving
        the home radius opens a trip, returning closes it as a completed loop. The
        response echoes any ``trip`` edge crossed so a client can react.
        """
        memory = ctx.store
        memory.set_location(payload.lat, payload.lon, payload.accuracy_m)
        response: dict[str, Any] = {"stored": True, "lat": payload.lat, "lon": payload.lon}
        if module_enabled("trip_tracking", resolved_settings):
            trip = process_location(memory, payload.lat, payload.lon)
            if trip["event"] is not None:
                response["trip"] = {
                    "event": trip["event"],
                    "trip_id": trip["trip_id"],
                    "distance_m": trip["distance_m"],
                }
        return response

    @router.get("/location", tags=["ingestion"])
    def location_get(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return the last-known position (or ``{"location": null}`` if unset)."""
        memory = ctx.store
        return {"location": memory.get_location()}

    # -- Closed-loop trip tracking -------------------------------------------

    @router.post(
        "/webhooks/home", status_code=status.HTTP_201_CREATED, tags=["ingestion"]
    )
    def home_set(
        payload: HomeSet,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set the home coordinate — the anchor closed-loop trip detection needs.

        Trip tracking is dormant until this is set: without a home fix there's no
        radius to cross, so nothing is auto-detected.
        """
        ctx.store.set_home(payload.lat, payload.lon)
        return {"stored": True, "lat": payload.lat, "lon": payload.lon}

    @router.get("/trips", tags=["ingestion"])
    def trips_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read-only snapshot of trips — **no side effects**, safe to poll.

        ``active`` is the open trip (if you're out right now); ``recent`` is the
        history newest-first; ``unlabeled`` are the completed trips still awaiting
        a label, i.e. the ones the system is asking you to name. ``categories`` is
        the suggested activity vocabulary; ``domains`` the life-sphere vocabulary
        (shop/work/home/personal) the focus-balance rollup buckets time-out by —
        both for the label form.
        """
        memory = ctx.store
        return {
            "active": memory.active_trip(),
            "recent": memory.recent_trips(limit=20),
            "unlabeled": memory.unlabeled_trips(limit=20),
            "categories": list(TRIP_CATEGORIES),
            "domains": list(FOCUS_DOMAINS),
        }

    @router.post("/webhooks/trip/label", tags=["ingestion"])
    def trip_label(
        payload: TripLabel,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Label, categorize, and (optionally) domain a completed trip.

        The retrospective ask. ``category`` is the activity flavor; ``domain`` is
        the life-sphere the focus-balance rollup sums time-out by. Both are
        optional — a bare label is fine, and the domain can be set later via
        ``/webhooks/trip/domain``.
        """
        label = payload.label.strip()
        if not label:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A trip label cannot be empty.",
            )
        updated = ctx.store.label_trip(
            payload.trip_id,
            label=label,
            category=normalize_trip_category(payload.category),
            domain=normalize_focus_domain(payload.domain),
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No trip with id {payload.trip_id}.",
            )
        return {
            "trip_id": updated["id"],
            "label": updated["label"],
            "category": updated["category"],
            "domain": updated["domain"],
        }

    @router.post("/webhooks/trip/domain", tags=["ingestion"])
    def trip_domain(
        payload: TripDomain,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """(Re)file a trip into a life-domain without touching its label.

        The focus-balance edit path: correct a misfiled trip, or add a domain to
        one you only labeled. A null/blank ``domain`` clears it.
        """
        updated = ctx.store.set_trip_domain(
            payload.trip_id, normalize_focus_domain(payload.domain)
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No trip with id {payload.trip_id}.",
            )
        return {"trip_id": updated["id"], "domain": updated["domain"]}

    @router.get("/balance", tags=["ingestion"])
    def focus_balance(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        days: int = 7,
    ) -> dict[str, Any]:
        """Focus-balance rollup — time out per life-domain over the last ``days``.

        Read-only, safe to poll. ``summary`` is the one-line digest; ``domains``
        the per-sphere breakdown with minutes, trip count, weekly target (scaled to
        the window), and whether it's running under half its aim.
        """
        days = max(1, min(days, 365))
        balance = build_focus_balance(ctx.store, days=days)
        return {
            "days": balance.days,
            "total_minutes": balance.total_minutes,
            "summary": balance_summary_line(balance),
            "domains": [
                {
                    "domain": d.domain,
                    "minutes": d.minutes,
                    "trips": d.trips,
                    "target_minutes": d.target_minutes,
                    "underserved": d.underserved,
                }
                for d in balance.domains
            ],
        }

    @router.post("/webhooks/trip/reflect", tags=["ingestion"])
    def trip_reflect(
        payload: TripReflect,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Submit a plain-English 'how it went' note and feed it into learning.

        The note is classified into an outcome (unless one is stated explicitly),
        which resolves the ``task`` episode the trip's return logged — so honest
        self-report becomes drift signal the learn pass reads. The raw note is
        also handed to the LLM-as-sensor, which may *propose* deeper structured
        updates (held pending for review; none if the model is offline).
        """
        memory = ctx.store
        reflection = payload.reflection.strip()
        if not reflection:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A reflection cannot be empty.",
            )
        trip = memory.get_trip(payload.trip_id)
        if trip is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No trip with id {payload.trip_id}.",
            )
        result = apply_reflection(
            memory,
            trip,
            reflection,
            outcome=payload.outcome,
            client=ollama_client,
            sensor_client=sensor_client,
        )
        return {
            "trip_id": payload.trip_id,
            "outcome": result["outcome"],
            "outcome_source": result["outcome_source"],
            "episode_resolved": result["episode_resolved"],
            "proposals": result["proposals"],
        }

    # -- Mail ingestion ------------------------------------------------------

    @router.post(
        "/webhooks/mail/sync", status_code=status.HTTP_201_CREATED, tags=["ingestion"]
    )
    def mail_sync(
        payload: MailSync,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Ingest a batch of messages for one account: dedup, triage, store.

        The retention policy comes from the account's configuration
        (``PREFRONTAL_MAIL_ACCOUNTS``) unless ``policy`` overrides it for this
        batch. Triage runs on the local Ollama model, falling back to the
        keyword heuristic if it's unavailable — so a down model never blocks the
        sync. Re-posting the same messages is idempotent (dedup on message id).
        """
        memory = ctx.store
        if not payload.account.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="mail sync requires a non-empty 'account'.",
            )
        policy = payload.policy or resolved_settings.policy_for(payload.account)
        summary = ingest_messages(
            memory,
            payload.messages,
            account=payload.account,
            policy=policy,
            client=triage_client,
            corrections=learned_corrections(
                memory,
                quick_drop_days=resolved_settings.triage_quick_drop_days,
                repeat_threshold=resolved_settings.triage_repeat_threshold,
            ),
            denylisted_senders=learned_denylist(
                memory,
                repeat_threshold=resolved_settings.triage_repeat_threshold,
            ),
            domain=resolved_settings.account_domain_map.get(payload.account),
        )
        return {
            "account": summary.account,
            "policy": summary.policy,
            "received": summary.received,
            "ingested": summary.ingested,
            "skipped": summary.skipped,
            "invalid": summary.invalid,
            "needs_action": summary.needs_action,
            "todos_created": summary.todos_created,
            "todos_suppressed": summary.todos_suppressed,
            "triaged_by_llm": summary.triaged_by_llm,
        }

    @router.get("/mail", tags=["ingestion"])
    def mail_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read-only snapshot: recent triaged mail and the open action items.

        ``needs_action`` lists messages still awaiting a reply/action (their
        linked todo is still open); ``recent`` is the latest ingested mail for a
        dashboard. No side effects — safe to poll.
        """
        memory = ctx.store
        return {
            "needs_action": memory.mail_needing_action(),
            "recent": memory.recent_mail(limit=30),
        }

    @router.get("/mail/triage/learned", tags=["ingestion"])
    def triage_learned(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """What triage has learned from your dropped intake todos.

        ``senders`` are repeat offenders (treated as no-action hints), ``recent``
        is the raw drop log (for curation), and ``prompt_addendum`` is the exact
        text appended to the triage system prompt on the next sync — so you can
        see, and ``forget``/``clear`` below, anything it picked up. Read-only.
        """
        memory = ctx.store
        return {
            "senders": memory.triage_dropped_senders(
                min_count=resolved_settings.triage_repeat_threshold
            ),
            "recent": memory.triage_feedback_list(limit=50),
            "prompt_addendum": learned_corrections(
                memory,
                quick_drop_days=resolved_settings.triage_quick_drop_days,
                repeat_threshold=resolved_settings.triage_repeat_threshold,
            ),
        }

    @router.post("/mail/triage/learned/forget", tags=["ingestion"])
    def triage_forget(
        payload: TriageForget,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Forget one learned correction (e.g. a drop that was really avoidance)."""
        removed = ctx.store.forget_triage_feedback(payload.id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No triage correction with id {payload.id}.",
            )
        return {"ok": True, "forgot": payload.id}

    @router.post("/mail/triage/learned/clear", tags=["ingestion"])
    def triage_clear(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Forget all learned corrections — resets triage to the base prompt."""
        return {"ok": True, "cleared": ctx.store.clear_triage_feedback()}

    # -- Location-Aware Task Anchor (Coffee Shop Nudge) ----------------------

    return router
