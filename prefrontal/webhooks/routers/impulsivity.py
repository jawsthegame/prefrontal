"""HTTP routes tagged "impulsivity".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    DEFAULT_PAUSE_SECONDS,
    SWITCH_ACTIONS,
    Annotated,
    CaptureImpulse,
    Depends,
    HTTPException,
    ImpulseCaptured,
    ScopedRequest,
    SwitchImpulse,
    SwitchPause,
    SwitchResolve,
    SwitchResolved,
    _impulse_captured_confirmation,
    _nudge_actions,
    _switch_resolved_confirmation,
    build_pause_message,
    infer_capture_title,
    pause_seconds,
    record_focus_switched,
    resolve_user,
    status,
    switch_response,
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
    """Build the "impulsivity" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post(
        "/webhooks/impulse/capture",
        response_model=ImpulseCaptured,
        status_code=status.HTTP_201_CREATED,
        tags=["impulsivity"],
    )
    def impulse_capture(
        payload: CaptureImpulse,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> ImpulseCaptured:
        """Park an impulsive idea as a ``source='impulse'`` todo and confirm back.

        The capture-and-defer intervention: when an impulse pulls at your
        attention, one tap stores it safely so the *fear of forgetting* — the
        thing that actually drives the switch — is relieved, without abandoning
        what you were doing. The raw text is kept verbatim in the todo's notes;
        a clean title is inferred (local LLM, heuristic fallback). It lands in
        the normal open-loop machinery (fit, briefing, avoidance) as any todo.

        Like ``/outing/start`` this is interactive — an iOS Shortcut waits on it
        — so titling shares the short inference budget and degrades fast.
        """
        memory = ctx.store
        raw = payload.impulse_text.strip()
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Send a non-empty impulse_text to capture.",
            )
        title = infer_capture_title(raw, client=ollama_client) or raw
        todo_id = memory.add_todo(
            title,
            notes=raw,
            priority=payload.priority,
            source="impulse",
        )
        return ImpulseCaptured(
            todo_id=todo_id,
            title=title,
            raw=raw,
            confirmation=_impulse_captured_confirmation(title),
        )

    # -- Focus sessions (Hyperfocus) -----------------------------------------

    @router.post(
        "/webhooks/focus/switch",
        response_model=SwitchPause,
        tags=["impulsivity"],
    )
    def focus_switch(
        payload: SwitchImpulse,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> SwitchPause:
        """The reflective-pause interception (Impulsivity): you feel the pull to switch.

        Records the *impulse* against the active focus block (the desk-bound twin
        of an outing — see docs/impulsivity.md) and returns a pause directive: the
        task you're on, how long you've been on it, a reflective-pause length, and
        the resolutions to offer. It does **not** change session state beyond the
        impulse counter — the user's choice is a separate ``/focus/resolve`` call,
        so a connection dropped mid-pause can't strand the session.

        With no active focus block there's nothing to switch *from*, so this 409s;
        the client should offer to start one, or just capture the thought via
        ``POST /webhooks/impulse/capture`` (which needs no block).
        """
        memory = ctx.store
        session = (
            memory.get_focus_session(payload.session_id)
            if payload.session_id is not None
            else memory.most_recent_active_focus_session()
        )
        if session is None or session.get("status") != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No active focus block to switch from. Start one with "
                    "/webhooks/focus/start, or capture the thought via "
                    "/webhooks/impulse/capture."
                ),
            )
        # The session may have come from get_focus_session (no elapsed) — recompute
        # from the active list so the pause scales on real elapsed time.
        elapsed = session.get("elapsed_minutes")
        if elapsed is None:
            match = next(
                (s for s in memory.active_focus_sessions() if s["id"] == session["id"]),
                None,
            )
            elapsed = (match or {}).get("elapsed_minutes") or 0.0
        memory.record_switch_impulse(session["id"])
        base = memory.get_float("pause_seconds", DEFAULT_PAUSE_SECONDS)
        name = ctx.user.get("display_name") or ""
        return SwitchPause(
            session_id=session["id"],
            intended_task=session["intended_task"],
            elapsed_minutes=round(elapsed, 1),
            pause_seconds=pause_seconds(elapsed, session.get("planned_minutes"), base),
            message=build_pause_message(session["intended_task"], elapsed, name=name),
            options=list(SWITCH_ACTIONS),
            # One-tap ntfy buttons that resolve the pause without leaving the app:
            # Stay on task / Park it / Switch anyway (empty if unconfigured).
            actions=_nudge_actions(
                resolved_settings,
                ctx.user.get("handle") or "",
                "pause",
                session["id"],
            ),
        )

    @router.post(
        "/webhooks/focus/resolve",
        response_model=SwitchResolved,
        tags=["impulsivity"],
    )
    def focus_resolve(
        payload: SwitchResolve,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> SwitchResolved:
        """Resolve a switch-impulse after the pause (Impulsivity).

        - **return** — friction worked; no state change beyond the impulse already
          counted. The block stays active.
        - **defer** — capture-and-defer: park ``impulse_text`` as a
          ``source='impulse'`` todo and count the deferral, then stay on the block.
        - **switch** — the user is moving on deliberately; close the block as
          ``switched`` and log the (shortened-but-real) episode.
        """
        memory = ctx.store
        try:
            action = switch_response(payload.action)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        session = (
            memory.get_focus_session(payload.session_id)
            if payload.session_id is not None
            else memory.most_recent_active_focus_session()
        )
        if session is None or session.get("status") != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No active focus block to resolve a switch against.",
            )
        session_id = session["id"]
        task = session["intended_task"]

        todo_id: int | None = None
        session_status = "active"
        title: str | None = None
        if action == "defer":
            raw = (payload.impulse_text or "").strip()
            if not raw:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="'defer' needs impulse_text to capture.",
                )
            title = infer_capture_title(raw, client=ollama_client) or raw
            todo_id = memory.add_todo(title, notes=raw, source="impulse")
            memory.mark_switch_deferred(session_id)
        elif action == "switch":
            closed = memory.close_focus_session(session_id, status="switched")
            if closed is not None:
                record_focus_switched(memory, closed)
            session_status = "switched"
        # 'return' is a no-op beyond what /focus/switch already counted.

        return SwitchResolved(
            session_id=session_id,
            action=action,
            todo_id=todo_id,
            session_status=session_status,
            confirmation=_switch_resolved_confirmation(action, task, title),
        )

    return router
