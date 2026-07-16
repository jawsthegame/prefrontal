"""HTTP routes tagged "assistant" — natural-language edits to todos/commitments.

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
    status,
)

from prefrontal.assistant import (
    build_snapshot,
    execute_actions,
    validate_actions,
)
from prefrontal.assistant import (
    plan as assistant_plan_message,
)
from prefrontal.availability import plan_availability, render_plan
from prefrontal.braindump import (
    CAPTURE_FEATURE,
    OnDeviceParse,
    plan_braindump,
)
from prefrontal.clock import local_datetime, parse_ts, utcnow
from prefrontal.integrations.anthropic import SUPPORTED_IMAGE_MEDIA_TYPES
from prefrontal.log import get_logger
from prefrontal.scheduling import window_config_for
from prefrontal.sensor import (
    avoided_state_keys,
    describe_proposal,
    record_candidates,
)
from prefrontal.vision import plan_vision
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    AssistantApply,
    AssistantMessage,
    BrainDumpMessage,
    FindTimeMessage,
    VisionMessage,
)
from prefrontal.webhooks.services import RouterServices

logger = get_logger(__name__)


def _strip_data_uri(image_base64: str) -> str:
    """Drop a leading ``data:...;base64,`` prefix, keeping just the payload.

    Native clients and Shortcuts sometimes send a full data URI; the SDK's image
    block wants only the base64 payload, so normalize here.
    """
    if image_base64.startswith("data:"):
        _, _, payload = image_base64.partition(",")
        return payload.strip()
    return image_base64.strip()


def build_router(services: RouterServices) -> APIRouter:
    """Build the "assistant" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    provider = services.provider

    @router.post("/assistant", tags=["assistant"])
    def assistant_interpret(
        payload: AssistantMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Turn a natural-language message into a **proposed** action list.

        Nothing is written. The response's ``actions`` are validated against the
        caller's current data (ids resolved, ops whitelisted); the client shows
        them and, on the user's confirmation, echoes them back to
        ``POST /assistant/apply``. ``errors`` explains anything that couldn't be
        turned into a supported edit.

        Uses Claude when the ``assistant`` agent is configured for the Anthropic
        provider and a key is available, otherwise the local Ollama model. A
        message that can't be provided at all yields 503.
        """
        client, provider_name = provider.select("assistant")
        result = assistant_plan_message(
            payload.message,
            ctx.store,
            client=client,
            now=utcnow(),
            tz=resolved_settings.timezone,
        )
        return {
            "reply": result.reply,
            "actions": [a.to_wire() for a in result.actions],
            "errors": result.errors,
            "provider": provider_name,
        }

    @router.post("/assistant/apply", tags=["assistant"])
    def assistant_apply(
        payload: AssistantApply,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Execute previously-proposed actions after re-validating them.

        The actions are re-checked against the *current* store (ids may have
        moved since they were proposed) before executing, and the scoped store
        re-verifies ownership on every write — so this can only ever touch the
        caller's own rows, and a stale action reports "nothing changed" rather
        than acting on the wrong item.
        """
        memory = ctx.store
        snapshot = build_snapshot(memory)
        actions, errors = validate_actions(payload.actions, snapshot)
        # A delegate_todo action writes its prep brief with the model; hand the same
        # provider-selected client the interpret step used (falls back to a heuristic
        # brief when unavailable). Other ops ignore it.
        client, _ = provider.select("assistant")
        results = execute_actions(
            memory, actions, timezone=resolved_settings.timezone, client=client
        )
        applied = sum(1 for r in results if r["ok"])
        return {"applied": applied, "results": results, "errors": errors}

    @router.post("/braindump", tags=["assistant"])
    def braindump(
        payload: BrainDumpMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Fan one unstructured ramble out to both capture paths (roadmap M1).

        A rambling voice note is the lowest-friction capture there is, but one
        dump mixes **actionable items** (todos, commitments, shopping, if-then
        plans, household facts) with **behavioral asides** ("I keep blowing off
        admin on Mondays"). This turns the single ``text`` into:

        - ``actions`` — a validated, previewable editing action list (the NL
          assistant). **Nothing is written**; the client shows them and, on
          confirmation, echoes them back to ``POST /assistant/apply``.
        - ``proposals`` — allowlisted behavioral candidates (the LLM sensor),
          recorded as **pending** proposals for review at ``GET /proposals`` and
          applied only on ``POST /proposals/{id}/accept``.

        So both halves keep their existing human-in-the-loop confirm step — a
        rambling, imperfect dump can never silently mutate the store.

        **Two ways in, one safety model.** Send raw ``text`` and the server parses
        it — Claude for a half whose agent (``assistant`` / ``sensor``) is opted
        into Anthropic, otherwise the local Ollama model. Or send ``parse`` — a
        structure the native app already extracted with its **on-device Foundation
        Model** (roadmap M1) — and the server calls **no model at all**, just
        validating the supplied actions/observations through the same gates
        (``provider`` reports ``"on_device"``). On-device parse is the cheap,
        private, offline path; raw text escalates to the opt-in cloud agent for the
        hard reasoning. A body with neither ``text`` nor ``parse`` is a 422; a
        ``parse`` that came back empty is a valid "found nothing" result, not an error.
        """
        memory = ctx.store
        parse = payload.parse
        if parse is not None:
            # On-device path: the client's Foundation Model already parsed the
            # ramble. Validate its structure through the identical propose→confirm
            # gates without touching a server model.
            result = plan_braindump(
                "",
                memory,
                parse=OnDeviceParse(
                    actions=parse.actions,
                    observations=parse.observations,
                    reply=parse.reply,
                ),
            )
            assistant_provider = sensor_provider = "on_device"
        else:
            if not payload.text.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Provide a non-empty 'text', or a 'parse'.",
                )
            # The two halves are independently provider-selectable agents, so they
            # can resolve to different providers (Claude for one, local Ollama for
            # the other); report each rather than one misleading label.
            assistant_client, assistant_provider = provider.select("assistant")
            sensor_client, sensor_provider = provider.select("sensor")
            result = plan_braindump(
                payload.text,
                memory,
                assistant_client=assistant_client,
                sensor_client=sensor_client,
                now=utcnow(),
                tz=resolved_settings.timezone,
                # Close the sensor's calibration loop: de-emphasize settings the
                # user reliably rejects (from the last learning pass) in the prompt.
                avoid_keys=avoided_state_keys(memory),
            )
        # Actions stay a preview (applied via /assistant/apply); the sensor
        # candidates are recorded pending here, exactly as POST /observe does, so
        # they land in the same review queue and apply via /proposals/{id}/accept.
        ids = set(record_candidates(memory, result.candidates))
        created = [p for p in memory.list_proposals("pending") if p["id"] in ids]
        # Persist which path handled this capture so /stats can chart the funnel
        # (on-device vs. escalated). `source` is the assistant provider — the parse
        # path reports "on_device" for both halves, so it's the honest capture-level
        # label; the two halves can differ only on the server path. Best-effort:
        # telemetry must never fail the capture (mirrors the shortcut recorder).
        try:
            memory.record_feature_event(
                CAPTURE_FEATURE, "invoked", source=assistant_provider
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
            logger.debug("record_feature_event(braindump) failed", exc_info=True)
        return {
            "reply": result.reply,
            "actions": [a.to_wire() for a in result.actions],
            "errors": result.errors,
            "proposals": [describe_proposal(p) for p in created],
            "provider": {"assistant": assistant_provider, "sensor": sensor_provider},
        }

    @router.post("/vision", tags=["assistant"])
    def vision(
        payload: VisionMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read one photo into structured items (roadmap: vision capture).

        A photo of anything already written down (a whiteboard, a school
        newsletter, a scribbled list, a receipt) is transcribed by the multimodal
        model and then fanned out through the **same** brain-dump paths, so the
        response has the identical shape and safety model as ``POST /braindump``:

        - ``transcript`` — what the model read off the image (for display/debug).
        - ``actions`` — a validated, previewable editing action list. **Nothing is
          written**; confirm via ``POST /assistant/apply``.
        - ``proposals`` — allowlisted behavioral candidates, recorded **pending**
          and applied only on ``POST /proposals/{id}/accept``.

        Vision is **local-first**: the on-device multimodal model reads the image
        when one is configured and installed, otherwise the cloud Anthropic model
        does (see :meth:`ProviderResolver.select_vision`). When *neither* backend
        can see it's a 503 — say so plainly rather than return a misleading empty
        plan. A blank image or an unsupported ``media_type`` is a 422. Downstream,
        the two brain-dump halves (``assistant`` / ``sensor``) still select their
        own provider.
        """
        memory = ctx.store
        image = _strip_data_uri(payload.image_base64)
        if not image:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Provide a non-empty 'image_base64'.",
            )
        # Validate the media type here rather than letting an unsupported value fall
        # through to describe_image (which raises) → transcribe_image (which swallows
        # the error) → a 200 with an empty plan indistinguishable from a blank image.
        media_type = payload.media_type.strip().lower()
        if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unsupported 'media_type' {payload.media_type!r}; expected one "
                    f"of {', '.join(sorted(SUPPORTED_IMAGE_MEDIA_TYPES))}."
                ),
            )
        # Local-first: prefer the on-device model, fall back to cloud. If neither
        # can see, there's no way to read the image, so say so plainly.
        vision_client, vision_provider = provider.select_vision()
        if vision_client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Vision needs a multimodal backend: set OLLAMA_VISION_MODEL "
                    "(and pull it) for on-device, or ANTHROPIC_API_KEY plus "
                    "'pip install prefrontal[anthropic]' for cloud."
                ),
            )
        assistant_client, assistant_provider = provider.select("assistant")
        sensor_client, sensor_provider = provider.select("sensor")
        result = plan_vision(
            image,
            media_type,
            memory,
            vision_client=vision_client,
            assistant_client=assistant_client,
            sensor_client=sensor_client,
            prompt=payload.prompt,
            now=utcnow(),
            tz=resolved_settings.timezone,
            avoid_keys=avoided_state_keys(memory),
        )
        # Same as /braindump: actions stay a preview; sensor candidates are recorded
        # pending here so they land in the shared review queue.
        ids = set(record_candidates(memory, result.candidates))
        created = [p for p in memory.list_proposals("pending") if p["id"] in ids]
        return {
            "transcript": result.transcript,
            "reply": result.reply,
            "actions": [a.to_wire() for a in result.actions],
            "errors": result.errors,
            "proposals": [describe_proposal(p) for p in created],
            "provider": {
                "vision": vision_provider,
                "assistant": assistant_provider,
                "sensor": sensor_provider,
            },
        }

    @router.post("/assistant/find-time", tags=["assistant"])
    def assistant_find_time(
        payload: FindTimeMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Find open calendar slots from a free-text ask — read-only.

        "Find 45 minutes for coffee with Sam this week", or "when are my wife and
        I both free for dinner tomorrow evening?" The message is parsed into a
        duration + timeframe + who's-involved (Claude when the ``assistant`` agent
        is configured for Anthropic, else the local Ollama model, else an offline
        heuristic), and open windows are computed from the calendar.

        **Who's involved shapes the constraints.** Your own commitments always
        block. A partner's whereabouts — the *FYI* events (where someone else will
        be) — block **only** when the plan involves them: "just me" ignores items
        that are only your partner's, while "the two of us" treats their calendar
        as a hard constraint too. ``ignored_fyi`` reports how many FYI items were
        left out so the client can offer to include them.

        When the ask is too vague to answer (almost always a missing duration),
        ``question`` is set and ``slots`` is empty — the caller should put the
        question back to the user and re-ask. Nothing is written either way.
        """
        client, provider_name = provider.select("assistant")
        memory = ctx.store
        tz = resolved_settings.timezone
        cfg = window_config_for(resolved_settings, memory)
        plan = plan_availability(
            payload.message,
            memory,
            client=client,
            now=utcnow(),
            tz=tz,
            awake_band=cfg.awake_band(),
            band_for_weekday=cfg.band_for_weekday,
        )

        def dump_slot(w: Any) -> dict[str, Any]:
            start_local = local_datetime(parse_ts(w.start), tz)
            end_local = local_datetime(parse_ts(w.end), tz)
            return {
                "start_at": w.start,
                "end_at": w.end,
                "minutes": w.minutes,
                "day": start_local.strftime("%a %b %-d"),
                "start": start_local.strftime("%-I:%M %p"),
                "end": end_local.strftime("%-I:%M %p"),
            }

        request = plan.request
        return {
            "question": plan.question,
            "request": (
                {
                    "minutes": request.minutes,
                    "days": request.days,
                    "with_partner": request.with_partner,
                    "title": request.title,
                    "time_window": list(request.time_window) if request.time_window else None,
                }
                if request is not None
                else None
            ),
            "slots": [dump_slot(w) for w in plan.slots],
            "with_partner": plan.with_partner,
            "considered": plan.considered,
            "ignored_fyi": plan.ignored_fyi,
            "text": render_plan(plan, tz),
            "provider": provider_name,
        }

    return router
