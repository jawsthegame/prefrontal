"""HTTP routes tagged "coaching".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes the
coaching agent's tick endpoint (``docs/coaching-agent.md`` §7) — the sibling of
``/webhooks/outing/check`` that fans over *every* enabled module.
"""
from __future__ import annotations

from prefrontal.coaching import (
    DEFAULT_ACK_WINDOW_MINUTES,
    build_context,
    collect_cues,
    decide,
    note_delivered,
    record_channel_outcome,
    record_fired,
    resolve_ack,
    sweep_stale_nudges,
)
from prefrontal.encouragement import (
    already_sent_today,
    assess_day,
    build_recovery,
    mark_sent_today,
    render_encouragement,
)
from prefrontal.modules import enabled_modules
from prefrontal.webhooks._common import (
    Annotated,
    Any,
    Depends,
    Request,
    ScopedRequest,
    resolve_user,
    utcnow,
)
from prefrontal.webhooks.notify import nudge_actions

#: Coaching ``context_key`` → (ntfy button ``kind``, ``cue.ref`` key holding the
#: target id), for cues whose tick delivery should carry one-tap action buttons.
#: Only cues listed here get ``actions``; others deliver as plain text. (The
#: per-module check endpoints attach their own buttons; this is the fan-over path.)
_CUE_ACTION_KIND = {"meal": ("meal", "target")}


def build_router(
    *,
    resolved_settings,
    n8n,
    ollama_client,
    summarizer_client,
    geocoder_client,
    _run_geocode,
):
    """Build the "coaching" APIRouter (shared services injected by create_app)."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/webhooks/coach/check", tags=["coaching"])
    async def coach_check(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Run one coaching tick: the cues due right now, each with a channel.

        n8n polls this like ``/webhooks/outing/check``, but it fans over *every*
        enabled module's :meth:`~prefrontal.modules.base.Module.evaluate` rather
        than being hard-wired to one. For each cue it applies channel choice and
        suppression (quiet hours + per-``dedup_key`` debounce), returns the
        fire-worthy decisions, and stamps each as fired so a standing cue won't
        repeat next poll (the general form of the anchor's fire-once guard).

        Body (optional): ``current_lat``/``current_lon`` ride into the
        :class:`~prefrontal.coaching.CoachContext` so location-aware evaluators
        keep working. Response: ``{"cues": [{module, intervention, urgency,
        channel, fire, text, context_key, dedup_key, ref}]}`` — n8n delivers each
        ``text`` on its ``channel``.
        """
        memory = ctx.store
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        now = utcnow()
        # Close last round's outcomes first: any nudge delivered earlier that was
        # never tapped within the ack window becomes a channel "miss", so the
        # nightly learn can shift channel choice (spec §8). Taps clear their own
        # marker (see /nudge/act), so what this sweeps really went unanswered.
        sweep_stale_nudges(
            memory,
            now,
            ack_window_minutes=memory.get_float(
                "coach_ack_window_minutes", DEFAULT_ACK_WINDOW_MINUTES
            ),
        )
        coach_ctx = build_context(
            memory,
            now=now,
            timezone=resolved_settings.timezone,
            display_name=ctx.user.get("display_name") or "",
            current_lat=body.get("current_lat"),
            current_lon=body.get("current_lon"),
        )
        modules = enabled_modules(resolved_settings)
        cues = collect_cues(memory, modules, coach_ctx)
        decisions = decide(memory, cues, coach_ctx)
        # Recorded at check time (not on confirmed delivery), matching how
        # outing/check advances last_level when it decides to fire.
        record_fired(memory, decisions, now)
        # Track the interactive nudges we're handing off so a later tap (or the
        # next sweep) can log which channel landed — the input to choose_channel.
        note_delivered(memory, decisions, now)
        handle = ctx.user.get("handle") or ""

        def _actions(d) -> list[dict[str, Any]]:
            """One-tap ntfy buttons for a cue whose kind is in _CUE_ACTION_KIND."""
            kind_ref = _CUE_ACTION_KIND.get(d.cue.context_key)
            if not kind_ref or not handle:
                return []
            kind, ref_key = kind_ref
            return nudge_actions(
                kind,
                (d.cue.ref or {}).get(ref_key),
                base_url=resolved_settings.oauth_base_url,
                secret=resolved_settings.session_secret,
                handle=handle,
            )

        return {
            "cues": [
                {
                    "module": d.cue.module,
                    "intervention": d.cue.intervention,
                    "urgency": d.cue.urgency,
                    "channel": d.channel,
                    "fire": True,
                    "text": d.text,
                    "context_key": d.cue.context_key,
                    "dedup_key": d.cue.dedup_key,
                    "ref": d.cue.ref,
                    "actions": _actions(d),
                }
                for d in decisions
            ]
        }

    @router.post("/webhooks/coach/ack", tags=["coaching"])
    async def coach_ack(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Report a delivered coaching nudge's outcome, feeding channel learning.

        The delivery layer calls this when a nudge is acted on (or explicitly
        marked ignored), so ``choose_channel`` can learn which channel lands
        (spec §8). Two shapes:

        - ``{"context", "target", "acknowledged"?}`` — resolve a *tracked*
          interactive nudge by the coordinates :func:`note_delivered` stamped
          (the usual path; ``/nudge/act`` calls this internally on a tap).
          ``acknowledged`` defaults to ``true``. ``resolved`` is ``false`` if no
          such nudge was tracked (a harmless late/duplicate report).
        - ``{"channel", "context", "acknowledged"}`` — an explicit outcome for a
          delivery path that knows the channel directly (e.g. a non-interactive
          channel with its own read receipt). Always logs.

        Both write a ``channel_response`` episode the nightly ``learn`` folds in.
        """
        memory = ctx.store
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        acknowledged = bool(body.get("acknowledged", True))
        context = body.get("context")
        target = body.get("target")
        channel = body.get("channel")

        if context and target is not None:
            resolved = resolve_ack(memory, str(context), target, acknowledged=acknowledged)
            return {"resolved": resolved}
        if channel and context:
            record_channel_outcome(
                memory, channel=str(channel), context=str(context), acknowledged=acknowledged
            )
            return {"resolved": True}
        return {"resolved": False, "detail": "need (context, target) or (channel, context)"}

    @router.get("/encouragement", tags=["coaching"])
    def encouragement(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Today's recovery message when the day's gone rough (model-free read).

        Pure and fast like ``/briefing``: assesses today's signals, and when
        ``rough`` builds a get-back-on-track plan (re-fit / defer / one small
        step) and renders it. ``already_sent`` reflects the once-per-day debounce
        cursor — the read never advances it, so a poller can check freely; call
        ``POST /encouragement/sent`` after actually delivering. Inert (``rough:
        false``) unless the ``encouragement`` coaching key is ``on``.
        """
        memory = ctx.store
        now = utcnow()
        assessment = assess_day(memory, now=now)
        plan = build_recovery(memory, assessment, now=now)
        return {
            "date": assessment.date,
            "rough": assessment.rough,
            "rough_score": assessment.rough_score,
            "already_sent": already_sent_today(memory, now=now),
            "signals": assessment.signals,
            "plan": {
                "refit": plan.refit,
                "defer": plan.defer,
                "first_step": plan.first_step,
            },
            "text": render_encouragement(assessment, plan),
        }

    @router.post("/encouragement/sent", tags=["coaching"])
    def encouragement_sent(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Stamp today as delivered so a later poll won't re-send (the §5 cursor).

        The delivering client (n8n) calls this only *after* a successful push, so
        the cursor reflects real delivery — keeping the read pure and capping
        encouragements at one per day.
        """
        now = utcnow()
        mark_sent_today(ctx.store, now=now)
        return {"already_sent": True, "date": now.strftime("%Y-%m-%d")}

    return router
