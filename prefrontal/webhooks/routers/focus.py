"""HTTP routes tagged "focus".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    DEFAULT_FOCUS_ABANDON_RATIO,
    DEFAULT_HARD_INTERRUPT_MINUTES,
    DEFAULT_SOFT_BLOCK_MINUTES,
    Annotated,
    Any,
    Depends,
    FocusEnd,
    FocusStart,
    FocusStarted,
    HTTPException,
    ScopedRequest,
    _focus_end_confirmation,
    _focus_started_confirmation,
    _nudge_actions,
    build_focus_message,
    focus_is_abandoned,
    focus_level,
    focus_level_rank,
    module_enabled,
    record_focus_abandoned,
    record_focus_end,
    resolve_user,
    should_protect,
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
    """Build the "focus" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post(
        "/webhooks/focus/start",
        response_model=FocusStarted,
        status_code=status.HTTP_201_CREATED,
        tags=["focus"],
    )
    def focus_start(
        payload: FocusStart,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> FocusStarted:
        """Declare a focus session: a stated task and an optional planned length.

        Unlike an outing, no window is *required* — the soft alignment check
        falls back to the ``hyperfocus_block_minutes`` default when no
        ``planned_minutes`` is given, and the hard biological break is keyed off
        ``hard_interrupt_minutes`` regardless. Only a blank task is rejected.
        """
        memory = ctx.store
        if not payload.intended_task.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Send a non-empty intended_task.",
            )
        session_id = memory.start_focus_session(
            payload.intended_task.strip(),
            planned_minutes=payload.planned_minutes,
            aligned=payload.aligned,
        )
        return FocusStarted(
            session_id=session_id,
            intended_task=payload.intended_task.strip(),
            planned_minutes=payload.planned_minutes,
            aligned=payload.aligned,
            confirmation=_focus_started_confirmation(
                payload.intended_task.strip(), payload.planned_minutes, payload.aligned
            ),
        )

    @router.post("/webhooks/focus/check", tags=["focus"])
    def focus_check(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Evaluate active focus sessions and report which interrupts are due.

        n8n polls this on a schedule. For each active session:

        - If elapsed time has blown far past the hard ceiling (the abandon
          ratio), the session is **auto-closed** as abandoned so a forgotten
          exit tap stops lingering active.
        - Otherwise the interrupt level is computed (``none``/``check``/
          ``break``); when a *new* level is crossed since the last poll it sets
          ``fire=true``, records the level so it fires once, and includes the
          ``message``.
        - ``protect`` reports whether an aligned, healthy block should currently
          shield the user from other modules' non-critical nudges — the
          asymmetry that makes this hyperfocus support rather than a timer.

        n8n acts on ``fire == true``; a delivery layer can consult ``protect``
        (or :func:`prefrontal.modules.hyperfocus.is_focus_protected`) to gate
        other nudges. A ``break`` is never suppressed.
        """
        memory = ctx.store
        # Hyperfocus owns focus-session interrupts; disabling it suppresses them.
        if not module_enabled("hyperfocus", resolved_settings):
            return {"active": [], "protect": False, "skipped": "module_disabled"}
        name = ctx.user.get("display_name") or ""
        soft = memory.get_float("hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
        hard = memory.get_float("hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
        abandon_ratio = memory.get_float(
            "focus_abandon_after_ratio", DEFAULT_FOCUS_ABANDON_RATIO
        )
        protect_enabled = memory.get_bool("protect_aligned_hyperfocus", True)

        results: list[dict[str, Any]] = []
        for session in memory.active_focus_sessions():
            elapsed = session["elapsed_minutes"] or 0.0
            planned = session["planned_minutes"]
            aligned = bool(session["aligned"])

            level, fire, message, outcome, protect = "none", False, "", None, False
            if focus_is_abandoned(elapsed, hard, abandon_ratio):
                closed = memory.close_focus_session(session["id"], status="abandoned")
                outcome = record_focus_abandoned(memory, closed)["outcome"]
                session_status = "abandoned"
            else:
                level = focus_level(
                    elapsed,
                    planned_minutes=planned,
                    soft_block_minutes=soft,
                    hard_interrupt_minutes=hard,
                )
                fire = focus_level_rank(level) > focus_level_rank(session["last_level"])
                protect = should_protect(
                    level, aligned=aligned, protect_enabled=protect_enabled
                )
                if fire:
                    memory.set_focus_session_level(session["id"], level)
                    message = build_focus_message(
                        level,
                        task=session["intended_task"],
                        elapsed_minutes=elapsed,
                        aligned=aligned,
                        name=name,
                    )
                session_status = "active"

            results.append(
                {
                    "session_id": session["id"],
                    "intended_task": session["intended_task"],
                    "elapsed_minutes": round(elapsed, 1),
                    "planned_minutes": planned,
                    "aligned": aligned,
                    "level": level,
                    "fire": fire,
                    "message": message,
                    "protect": protect,
                    "status": session_status,
                    "outcome": outcome,
                    # One-tap ntfy button: Wrap up (empty if unconfigured / closed).
                    "actions": (
                        _nudge_actions(
                            resolved_settings,
                            ctx.user.get("handle") or "",
                            "focus",
                            session["id"],
                        )
                        if session_status == "active"
                        else []
                    ),
                }
            )
        return {"active": results, "protect": any(r["protect"] for r in results)}

    @router.post("/webhooks/focus/end", tags=["focus"])
    def focus_end(
        payload: FocusEnd,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Close a focus session and log planned-vs-actual for pattern tracking.

        Records the actual time spent as a ``task`` episode (predicted = planned
        duration, actual = minutes spent) so the time-blindness learning can use
        it. A captured ``breadcrumb`` and one-tap ``outcome`` rating ride along
        on the session row and the episode notes.
        """
        memory = ctx.store
        session_id = payload.session_id
        if session_id is None:
            recent = memory.most_recent_active_focus_session()
            if recent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active focus session to end.",
                )
            session_id = recent["id"]

        closed = memory.close_focus_session(
            session_id,
            status=payload.status,
            breadcrumb=payload.breadcrumb,
            outcome=payload.outcome,
        )
        if closed is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Focus session {session_id} is not active.",
            )

        if payload.status == "ended":
            recorded = record_focus_end(
                memory, closed, outcome=payload.outcome, breadcrumb=payload.breadcrumb
            )
        else:
            recorded = record_focus_abandoned(memory, closed)
        actual = closed.get("actual_minutes")
        actual = round(actual, 1) if actual is not None else None
        planned = closed.get("planned_minutes")
        return {
            "session_id": session_id,
            "status": payload.status,
            "actual_minutes": actual,
            "planned_minutes": planned,
            "breadcrumb": closed.get("breadcrumb"),
            "outcome": recorded["outcome"],
            "episode_id": recorded["episode_id"],
            "confirmation": _focus_end_confirmation(payload.status, actual, planned),
        }

    @router.get("/focus", tags=["focus"])
    def focus_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read-only snapshot of focus sessions for monitoring — **no side effects**.

        Unlike ``/webhooks/focus/check`` (which fires interrupts and auto-closes
        abandoned sessions), this never mutates state, so a dashboard can poll it
        freely. Active sessions carry their current interrupt ``level`` and
        ``protect`` flag (computed, not recorded); recent sessions are returned
        newest first for history.
        """
        memory = ctx.store
        soft = memory.get_float("hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
        hard = memory.get_float("hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
        protect_enabled = memory.get_bool("protect_aligned_hyperfocus", True)
        active = []
        for s in memory.active_focus_sessions():
            level = focus_level(
                s.get("elapsed_minutes") or 0.0,
                planned_minutes=s["planned_minutes"],
                soft_block_minutes=soft,
                hard_interrupt_minutes=hard,
            )
            active.append(
                {
                    "session_id": s["id"],
                    "intended_task": s["intended_task"],
                    "elapsed_minutes": round(s.get("elapsed_minutes") or 0.0, 1),
                    "planned_minutes": s["planned_minutes"],
                    "aligned": bool(s["aligned"]),
                    "level": level,
                    "protect": should_protect(
                        level, aligned=bool(s["aligned"]), protect_enabled=protect_enabled
                    ),
                    "started_at": s["started_at"],
                }
            )
        recent = [
            {
                "session_id": s["id"],
                "intended_task": s["intended_task"],
                "status": s["status"],
                "planned_minutes": s["planned_minutes"],
                "started_at": s["started_at"],
                "ended_at": s.get("ended_at"),
                "outcome": s.get("outcome"),
                "breadcrumb": s.get("breadcrumb"),
            }
            for s in memory.recent_focus_sessions(limit=20)
        ]
        return {"active": active, "recent": recent}

    # -- Commitments (schedule for impact analysis) --------------------------

    return router
