"""HTTP routes tagged "ingestion".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    ACTION_OUTCOME,
    Annotated,
    Any,
    Depends,
    EpisodeCreated,
    HTTPException,
    LocationPing,
    MailSync,
    Request,
    ScopedRequest,
    ShortcutPayload,
    TriageForget,
    ingest_messages,
    learned_corrections,
    learned_denylist,
    parse_inbound_event,
    resolve_user,
    status,
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
    """Build the "ingestion" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

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
        """Receive an event pushed by an n8n workflow.

        Currently classifies the payload via
        :func:`prefrontal.integrations.n8n.parse_inbound_event` and echoes the
        routing decision. Concrete per-event handlers are a documented TODO.
        Authenticated like the other webhooks (no per-user data is touched yet).
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {"value": body}
        return parse_inbound_event(body)

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
        """
        memory = ctx.store
        memory.set_location(payload.lat, payload.lon, payload.accuracy_m)
        return {"stored": True, "lat": payload.lat, "lon": payload.lon}

    @router.get("/location", tags=["ingestion"])
    def location_get(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return the last-known position (or ``{"location": null}`` if unset)."""
        memory = ctx.store
        return {"location": memory.get_location()}
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
            client=ollama_client,
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
