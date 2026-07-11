"""HTTP routes tagged "coaching".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes the
coaching agent's tick endpoint (``docs/coaching-agent.md`` §7) — the sibling of
``/webhooks/outing/check`` that fans over *every* enabled module.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
    Request,
)

from prefrontal.coaching import (
    record_channel_outcome,
    resolve_ack,
    run_coaching_tick,
)
from prefrontal.encouragement import (
    already_sent_today,
    assess_day,
    build_recovery,
    mark_sent_today,
    render_encouragement,
)
from prefrontal.impact import (
    utcnow,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.notify import (
    alarm_actions_for_cue,
    nudge_actions,
    trip_label_actions,
)
from prefrontal.webhooks.services import RouterServices

#: Coaching ``context_key`` → (ntfy button ``kind``, ``cue.ref`` key holding the
#: target id), for cues whose tick delivery should carry one-tap action buttons.
#: Only cues listed here get ``actions``; others deliver as plain text. (The
#: per-module check endpoints attach their own buttons; this is the fan-over path.)
_CUE_ACTION_KIND = {
    "meal": ("meal", "target"),
    "water": ("water", "target"),
    "meds": ("meds", "target"),
    "biobreak": ("biobreak", "target"),
    "winddown": ("winddown", "target"),
    "movement": ("movement", "target"),
    # "trip" is handled separately (per-user quick-file domains) — see _actions.
    "away_proposal": ("away", "trip_id"),
}


def build_router(services: RouterServices) -> APIRouter:
    """Build the "coaching" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings

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

        # The whole tick — sweeps (in the right order, before cue collection),
        # collect, decide, and record — lives in one shared service so this
        # endpoint and the `prefrontal coach` CLI can't drift.
        result = run_coaching_tick(
            memory,
            settings=resolved_settings,
            ollama=services.ollama,
            current_lat=body.get("current_lat"),
            current_lon=body.get("current_lon"),
            display_name=ctx.user.get("display_name") or "",
        )
        handle = ctx.user.get("handle") or ""

        def _actions(d) -> list[dict[str, Any]]:
            """One-tap ntfy buttons for a cue whose kind is in _CUE_ACTION_KIND."""
            # Morning-prep's "Set alarm" button is a client-side view action built
            # from the cue's ref — no signing/handle needed (see notify.py).
            if d.cue.context_key == "morning_prep":
                return alarm_actions_for_cue(d.cue)
            ref = d.cue.ref or {}
            # The trip-label ask's file-into-sphere buttons are per-user (the ≤3
            # configured quick-file domains, stamped on the cue ref), not a fixed set.
            if d.cue.context_key == "trip" and handle:
                return trip_label_actions(
                    ref.get("quick_domains"),
                    ref.get("trip_id"),
                    base_url=resolved_settings.oauth_base_url,
                    secret=resolved_settings.session_secret,
                    handle=handle,
                )
            kind_ref = _CUE_ACTION_KIND.get(d.cue.context_key)
            if not kind_ref or not handle:
                return []
            kind, ref_key = kind_ref
            return nudge_actions(
                kind,
                ref.get(ref_key),
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
                for d in result.decisions
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
