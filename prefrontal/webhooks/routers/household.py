"""HTTP routes tagged "household" — the shared co-parent sheet + its editing API.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Read via
``GET /household/sheet``; the deterministic write endpoints back the ``/kids``
dashboard's inline forms (the plain-English box uses ``/assistant`` instead).
Every route is scoped to the caller and guarded to household members — a caller
in no household gets 404, not the store's raw scope error.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from prefrontal.commitments import KIND_CHILD, to_utc
from prefrontal.household import (
    award_stars_and_notify,
    normalize_prompt,
    parse_structured,
    prompt_due,
    prompt_question,
)
from prefrontal.memory.repos.household import (
    AGREEMENT_KINDS,
    FACT_CATEGORIES,
    normalize_fact_category,
    normalize_fact_item,
)
from prefrontal.webhooks._common import (
    AgreementSet,
    Annotated,
    Any,
    AppointmentCreate,
    ChildCreate,
    ChildRename,
    Depends,
    FactClear,
    FactSet,
    HTTPException,
    PromptConfig,
    ScopedRequest,
    StarAward,
    _parse_dt_or_none,
    build_sheet,
    local_datetime,
    render_sheet,
    resolve_user,
    status,
    utcnow,
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
    """Build the "household" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    def _require_member(ctx: ScopedRequest) -> None:
        """404 if the caller isn't in a household (nothing shared to touch)."""
        if ctx.store.household_id_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You're not set up in a household.",
            )

    def _child_name(ctx: ScopedRequest, child_id: int) -> str | None:
        """The roster name for ``child_id`` (``None`` for household-wide id 0)."""
        if not child_id:
            return None
        return next(
            (c["name"] for c in ctx.store.children() if c["id"] == child_id), None
        )

    def _valid_category(value: str) -> str:
        cat = normalize_fact_category(value)
        if cat is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="category must be one of " + ", ".join(FACT_CATEGORIES),
            )
        return cat

    # -- read -----------------------------------------------------------------

    @router.get("/household/sheet", tags=["household"])
    def household_sheet(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return the caller's shared household sheet — structured + rendered Markdown.

        Both co-parents see the same rows (household-scoped, not per-user).
        Deterministic assembly, built on request — no cache. ``vocab`` drives the
        ``/kids`` dashboard's category / kind dropdowns off the server.
        """
        _require_member(ctx)
        sheet = build_sheet(ctx.store)
        return {
            "sheet": asdict(sheet),
            "markdown": render_sheet(sheet),
            "vocab": {
                "fact_categories": list(FACT_CATEGORIES),
                "agreement_kinds": list(AGREEMENT_KINDS),
            },
        }

    # -- roster ---------------------------------------------------------------

    @router.post("/household/children", status_code=status.HTTP_201_CREATED, tags=["household"])
    def add_child(
        payload: ChildCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add a kid to the roster (idempotent on name)."""
        _require_member(ctx)
        cid = ctx.store.add_child(name=payload.name, birthday=payload.birthday)
        return {"id": cid, "name": payload.name}

    @router.post("/household/children/{child_id}", tags=["household"])
    def rename_child(
        child_id: int,
        payload: ChildRename,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Rename a kid (and optionally set a birthday)."""
        _require_member(ctx)
        if not ctx.store.rename_child(child_id, name=payload.name, birthday=payload.birthday):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such child")
        return {"id": child_id, "name": payload.name}

    # -- facts ----------------------------------------------------------------

    @router.post("/household/facts", tags=["household"])
    def set_fact(
        payload: FactSet,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Upsert one fact, stamping the acting user as ``updated_by``."""
        _require_member(ctx)
        category = _valid_category(payload.category)
        item = normalize_fact_item(payload.item)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="item must be non-empty",
            )
        ctx.store.set_fact(
            category=category, item=item, value=payload.value,
            updated_by=ctx.user["id"], child_id=payload.child_id,
        )
        return {"ok": True, "category": category, "item": item}

    @router.post("/household/facts/clear", tags=["household"])
    def clear_fact(
        payload: FactClear,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Delete one fact. ``removed`` is false if it was already gone."""
        _require_member(ctx)
        removed = ctx.store.clear_fact(
            category=_valid_category(payload.category),
            item=normalize_fact_item(payload.item),
            child_id=payload.child_id,
        )
        return {"removed": removed}

    # -- agreements -----------------------------------------------------------

    @router.post("/household/agreements", tags=["household"])
    def set_agreement(
        payload: AgreementSet,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Upsert a standing behaviour plan (star charts via ``structured``)."""
        _require_member(ctx)
        if payload.kind not in AGREEMENT_KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="kind must be one of " + ", ".join(AGREEMENT_KINDS),
            )
        import json

        structured = json.dumps(payload.structured) if payload.structured is not None else None
        aid = ctx.store.set_agreement(
            title=payload.title, body=payload.body, kind=payload.kind,
            structured=structured, updated_by=ctx.user["id"], child_id=payload.child_id,
        )
        return {"id": aid, "title": payload.title}

    @router.post("/household/agreements/{agreement_id}/remove", tags=["household"])
    def remove_agreement(
        agreement_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Delete an agreement by id (scoped to the household)."""
        _require_member(ctx)
        if not ctx.store.remove_agreement(agreement_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such agreement")
        return {"removed": True}

    # -- star chart ------------------------------------------------------------

    @router.post("/household/agreements/{agreement_id}/stars", tags=["household"])
    def award_stars(
        agreement_id: int,
        payload: StarAward,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Record earned stars against a chart; congratulate + notify both parents.

        Appends to the star ledger, then compares the running total before and
        after: any reward threshold the grant *crossed* is "reached", and for each
        one a celebration is pushed to **every** household member (both parents),
        so a goal hit is never something only the tracking parent knows. Returns
        the new total, the goals reached, the next reward, and who was notified.
        """
        _require_member(ctx)
        if payload.delta == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="delta must be non-zero",
            )
        try:
            result = award_stars_and_notify(
                ctx.store,
                agreement_id,
                delta=payload.delta,
                awarded_by=ctx.user["id"],
                note=payload.note,
                settings=resolved_settings,
            )
        except ValueError as exc:  # earn-only chart, negative delta
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from None
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such agreement"
            )
        return {
            "total": result["total"],
            "delta": result["delta"],
            "goals_reached": result["goals_reached"],
            "next_goal": result["next_goal"],
            "notified": result["notified"],
        }

    @router.post("/household/agreements/{agreement_id}/prompt", tags=["household"])
    def set_prompt(
        agreement_id: int,
        payload: PromptConfig,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set a chart's recurring award-prompt schedule (weekdays + time of day).

        Merges the validated ``prompt`` block into the agreement's ``structured``
        JSON (leaving the thresholds untouched), so the periodic sweep can ask both
        parents "did <kid> earn a star today?" on the chosen days.
        """
        _require_member(ctx)
        agreement = ctx.store.agreement(agreement_id)
        if agreement is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such agreement"
            )
        clean, error = normalize_prompt(payload.model_dump())
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
            )
        structured = parse_structured(agreement.get("structured"))
        structured = structured if isinstance(structured, dict) else {}
        structured["prompt"] = clean
        import json

        ctx.store.set_agreement(
            title=agreement["title"],
            body=agreement.get("body"),
            kind=agreement.get("kind") or "consistency",
            structured=json.dumps(structured),
            updated_by=ctx.user["id"],
            child_id=agreement.get("child_id") or 0,
        )
        return {"ok": True, "prompt": clean}

    @router.post("/webhooks/household/star-prompts/check", tags=["household"])
    def star_prompts_check(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Fire any star-award prompts due now for the caller's household.

        A periodic trigger (n8n / launchd, like the coach/panic checks) POSTs here;
        the sweep finds every chart whose schedule is due, sends both parents a
        one-tap "did <kid> earn a star?" push, and stamps ``last_prompted_at`` so it
        fires once per local day. Idempotent — a second call the same day is a no-op.
        """
        _require_member(ctx)
        from prefrontal.integrations.delivery import (
            deliver_to_household,
            household_prompt_notice,
        )

        now = utcnow()
        now_local = local_datetime(now, resolved_settings.timezone)
        hid = ctx.store.household_id_or_none()
        sent: list[dict[str, Any]] = []
        for agreement in ctx.store.agreements():
            structured = parse_structured(agreement.get("structured"))
            last_local = None
            if agreement.get("last_prompted_at"):
                last_dt = _parse_dt_or_none(agreement["last_prompted_at"])
                last_local = (
                    local_datetime(last_dt, resolved_settings.timezone)
                    if last_dt else None
                )
            if not prompt_due(structured, now_local=now_local, last_prompted_local=last_local):
                continue
            child_name = _child_name(ctx, agreement.get("child_id") or 0)
            question = prompt_question(structured, child_name, agreement["title"])
            notified = deliver_to_household(
                ctx.store,
                hid,
                household_prompt_notice(question, agreement["id"], channel="push"),
                settings=resolved_settings,
                base_url=resolved_settings.oauth_base_url,
                secret=resolved_settings.session_secret,
            )
            ctx.store.mark_prompted(agreement["id"])
            sent.append(
                {"agreement_id": agreement["id"], "title": agreement["title"],
                 "question": question, "notified": notified}
            )
        return {"sent": sent, "checked_at": now_local.strftime("%Y-%m-%d %H:%M")}

    # -- appointments ---------------------------------------------------------

    @router.post(
        "/household/appointments", status_code=status.HTTP_201_CREATED, tags=["household"]
    )
    def add_appointment(
        payload: AppointmentCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add a kid appointment as a ``kind='child'`` commitment on the caller's calendar.

        The shared sheet surfaces upcoming child appointments across the caller's
        calendar; other co-parents see them from their own view when they sync the
        same event (v1 keeps commitments per-user).
        """
        _require_member(ctx)
        try:
            start_at = to_utc(payload.start_at, default_tz=resolved_settings.timezone)
            end_at = (
                to_utc(payload.end_at, default_tz=resolved_settings.timezone)
                if payload.end_at else None
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"couldn't parse the time: {exc}",
            ) from None
        cid, created = ctx.store.upsert_commitment(
            title=payload.title, start_at=start_at, end_at=end_at,
            location=payload.location, source="manual",
            kind=KIND_CHILD, kind_source="user",
        )
        return {"id": cid, "created": created, "title": payload.title}

    return router
