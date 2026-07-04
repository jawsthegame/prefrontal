"""HTTP routes tagged "household" — the shared co-parent sheet + its editing API.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Read via
``GET /household/sheet``; the deterministic write endpoints back the ``/kids``
dashboard's inline forms (the plain-English box uses ``/assistant`` instead).
Every route is scoped to the caller and guarded to household members — a caller
in no household gets 404, not the store's raw scope error.
"""
from __future__ import annotations

from dataclasses import asdict
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
    status,
)

from prefrontal.clock import TS_FMT
from prefrontal.clock import (
    parse_ts as _parse_dt_or_none,
)
from prefrontal.commitments import KIND_CHILD, to_utc
from prefrontal.household import (
    BALANCE_WINDOW_DAYS,
    award_stars_and_notify,
    balance_view,
    build_sheet,
    checkin_due,
    checkin_question,
    digest_interval_ok,
    digest_message,
    normalize_checkin_config,
    normalize_chore,
    normalize_prompt,
    normalize_routine,
    parse_star_tiers,
    parse_structured,
    prompt_due,
    prompt_question,
    render_sheet,
    run_chores_check,
    unseen_changes,
    week_key,
)
from prefrontal.impact import (
    utcnow,
)
from prefrontal.memory.repos.household import (
    AGREEMENT_KINDS,
    FACT_CATEGORIES,
    normalize_fact_category,
    normalize_fact_item,
)
from prefrontal.scheduling import (
    local_datetime,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    require_member,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    AgreementSet,
    AppointmentCreate,
    BalanceConfig,
    CheckinConfig,
    ChildCreate,
    ChildRename,
    ChoreEnabled,
    ChoreSet,
    DigestConfig,
    FactClear,
    FactSet,
    HouseholdCreate,
    InviteRedeem,
    PromptConfig,
    RoutineEnabled,
    RoutineSet,
    ShoppingAdd,
    ShoppingGot,
    StarAward,
    TierConfig,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "household" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings

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
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Return the caller's shared household sheet — structured + rendered Markdown.

        Both co-parents see the same rows (household-scoped, not per-user).
        Deterministic assembly, built on request — no cache. ``vocab`` drives the
        ``/kids`` dashboard's category / kind dropdowns off the server.
        """
        now = utcnow()
        sheet = build_sheet(ctx.store, now=now, timezone=resolved_settings.timezone)
        now_local = local_datetime(now, resolved_settings.timezone)
        week = week_key(now_local)
        config = ctx.store.get_checkin_config()
        checkin = {
            "enabled": config["enabled"],
            "day": config["day"],
            "time": config["time"],
            "week": week,
            "responses": [
                {"by_name": r["by_name"], "response": r["response"]}
                for r in ctx.store.checkin_responses(week)
            ],
        }
        # The delta digest: count what the other parent changed since this parent
        # last looked, then stamp "seen now" — opening the sheet *is* catching up.
        since = max(
            ctx.store.get_state("household_seen_at") or "",
            ctx.store.get_state("household_digested_at") or "",
        )
        unseen = unseen_changes(ctx.store, viewer_id=ctx.user["id"], since=since)
        digest = {"enabled": ctx.store.get_digest_enabled(), "unseen": len(unseen)}
        ctx.store.set_state(
            "household_seen_at", now.strftime(TS_FMT), source="inferred"
        )
        # The load-balance view (opt-in, shared-only). Derived on read from a
        # 30-day provenance window; `view` is null when the toggle is off.
        shared = ctx.store.is_shared_household()
        balance = None
        if shared:
            bal_enabled = ctx.store.get_balance_enabled()
            view = None
            if bal_enabled:
                since = (now - timedelta(days=BALANCE_WINDOW_DAYS)).strftime(
                    TS_FMT
                )
                view = balance_view(
                    ctx.store.contribution_counts(since),
                    carrying=ctx.store.accountability_counts(),
                )
            balance = {"enabled": bal_enabled, "window_days": BALANCE_WINDOW_DAYS, "view": view}
        # The parents themselves (id + display name), so the /kids UI can offer
        # them in the chore-owner / routine-accountable pickers.
        members = [
            {"id": m["id"], "name": m.get("display_name") or m["handle"]}
            for m in ctx.store.household_members(ctx.store.household_id_or_none())
            if m.get("status") in (None, "active")
        ]
        return {
            "sheet": asdict(sheet),
            "markdown": render_sheet(sheet),
            "invites": ctx.store.pending_invites(),
            # A household of one is fully supported; the co-parent-only features
            # (check-in, digest, balance) hide when not shared and return
            # automatically once a second parent joins.
            "shared": shared,
            "members": members,
            "checkin": checkin,
            "digest": digest,
            "balance": balance,
            "vocab": {
                "fact_categories": list(FACT_CATEGORIES),
                "agreement_kinds": list(AGREEMENT_KINDS),
            },
        }

    # -- self-serve membership (no operator needed) ---------------------------

    @router.post("/household/create", status_code=status.HTTP_201_CREATED, tags=["household"])
    def create_own_household(
        payload: HouseholdCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Create a household and join it — the self-serve alternative to the operator flow."""
        name = payload.name.strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name must be non-empty"
            )
        try:
            hid = ctx.store.create_own_household(name)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        return {"id": hid, "name": name}

    @router.post("/household/invites", status_code=status.HTTP_201_CREATED, tags=["household"])
    def create_invite(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Mint a shareable invite code (+ a join link) for the caller's household."""
        invite = ctx.store.create_invite()
        base = resolved_settings.oauth_base_url
        join_url = f"{base}/kids?invite={invite['code']}" if base else ""
        return {**invite, "join_url": join_url}

    @router.post("/household/invites/{invite_id}/revoke", tags=["household"])
    def revoke_invite(
        invite_id: int,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Revoke an unredeemed invite code."""
        if not ctx.store.revoke_invite(invite_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such invite")
        return {"revoked": True}

    @router.post("/household/invites/redeem", tags=["household"])
    def redeem_invite(
        payload: InviteRedeem,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Join a household by redeeming an invite code (no operator step)."""
        result = ctx.store.redeem_invite(payload.code)
        if not result["ok"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=result["error"]
            )
        return {"ok": True, "household_name": result.get("household_name")}

    # -- roster ---------------------------------------------------------------

    @router.post("/household/children", status_code=status.HTTP_201_CREATED, tags=["household"])
    def add_child(
        payload: ChildCreate,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Add a kid to the roster (idempotent on name)."""
        cid = ctx.store.add_child(name=payload.name, birthday=payload.birthday)
        return {"id": cid, "name": payload.name}

    @router.post("/household/children/{child_id}", tags=["household"])
    def rename_child(
        child_id: int,
        payload: ChildRename,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Rename a kid (and optionally set a birthday)."""
        if not ctx.store.rename_child(child_id, name=payload.name, birthday=payload.birthday):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such child")
        return {"id": child_id, "name": payload.name}

    # -- facts ----------------------------------------------------------------

    @router.post("/household/facts", tags=["household"])
    def set_fact(
        payload: FactSet,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Upsert one fact, stamping the acting user as ``updated_by``."""
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
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Delete one fact. ``removed`` is false if it was already gone."""
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
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Upsert a standing behaviour plan (star charts via ``structured``)."""
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
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Delete an agreement by id (scoped to the household)."""
        if not ctx.store.remove_agreement(agreement_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such agreement")
        return {"removed": True}

    # -- star chart ------------------------------------------------------------

    @router.post("/household/agreements/{agreement_id}/stars", tags=["household"])
    def award_stars(
        agreement_id: int,
        payload: StarAward,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Record earned stars against a chart; congratulate + notify both parents.

        Appends to the star ledger, then compares the running total before and
        after: any reward threshold the grant *crossed* is "reached", and for each
        one a celebration is pushed to **every** household member (both parents),
        so a goal hit is never something only the tracking parent knows. Returns
        the new total, the goals reached, the next reward, and who was notified.
        """
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
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Set a chart's recurring award-prompt schedule (weekdays + time of day).

        Merges the validated ``prompt`` block into the agreement's ``structured``
        JSON (leaving the thresholds untouched), so the periodic sweep can ask both
        parents "did <kid> earn a star today?" on the chosen days.
        """
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

    @router.post("/household/agreements/{agreement_id}/tiers", tags=["household"])
    def set_tiers(
        agreement_id: int,
        payload: TierConfig,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Set/replace a chart's reward tiers in place (e.g. '7=small LEGO, 30=large').

        Parses the comma-separated ``count=reward`` spec and merges the thresholds
        into the agreement's ``structured`` JSON, leaving any prompt schedule and
        the running star total untouched — so you can add or retune tiers without
        recreating the chart (and a plain agreement becomes a star chart).
        """
        agreement = ctx.store.agreement(agreement_id)
        if agreement is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such agreement"
            )
        tiers = parse_star_tiers(payload.tiers)
        if tiers is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Give at least one tier as 'count=reward', e.g. '7=small LEGO, 30=large'.",
            )
        structured = parse_structured(agreement.get("structured"))
        structured = structured if isinstance(structured, dict) else {}
        structured["thresholds"] = tiers
        structured.setdefault("unit", "star")
        structured.setdefault("earn_only", True)
        import json

        ctx.store.set_agreement(
            title=agreement["title"],
            body=agreement.get("body"),
            kind=agreement.get("kind") or "consistency",
            structured=json.dumps(structured),
            updated_by=ctx.user["id"],
            child_id=agreement.get("child_id") or 0,
        )
        return {"ok": True, "thresholds": tiers}

    @router.post("/webhooks/household/star-prompts/check", tags=["household"])
    def star_prompts_check(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Fire any star-award prompts due now for the caller's household.

        A periodic trigger (n8n / launchd, like the coach/panic checks) POSTs here;
        the sweep finds every chart whose schedule is due, sends both parents a
        one-tap "did <kid> earn a star?" push, and stamps ``last_prompted_at`` so it
        fires once per local day. Idempotent — a second call the same day is a no-op.
        """
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

    # -- weekly mental-load check-in ------------------------------------------

    @router.post("/household/checkin", tags=["household"])
    def set_checkin(
        payload: CheckinConfig,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Set the opt-in weekly mental-load check-in schedule (both parents share it)."""
        clean, error = normalize_checkin_config(payload.model_dump())
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
            )
        ctx.store.set_checkin_config(
            enabled=clean["enabled"], day=clean["day"], time=clean["time"]
        )
        return {"ok": True, "checkin": clean}

    @router.post("/webhooks/household/checkin/check", tags=["household"])
    def checkin_check(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Send the weekly check-in to both parents if it's due (once per ISO week).

        A periodic trigger (n8n / launchd) POSTs here; when the schedule is due the
        endpoint pushes both parents the warm one-tap self-report and stamps
        ``checkin_last_sent_at`` so it fires once that week. Idempotent within a week.
        """
        now_local = local_datetime(utcnow(), resolved_settings.timezone)
        # Load-balancing is a two-parent concern — a solo household skips it.
        if not ctx.store.is_shared_household():
            return {"sent": False, "checked_at": now_local.strftime("%Y-%m-%d %H:%M")}
        from prefrontal.integrations.delivery import (
            deliver_to_household,
            household_checkin_notice,
        )

        config = ctx.store.get_checkin_config()
        last_local = None
        if config.get("last_sent_at"):
            last_dt = _parse_dt_or_none(config["last_sent_at"])
            last_local = (
                local_datetime(last_dt, resolved_settings.timezone) if last_dt else None
            )
        if not checkin_due(config, now_local=now_local, last_sent_local=last_local):
            return {"sent": False, "checked_at": now_local.strftime("%Y-%m-%d %H:%M")}
        notified = deliver_to_household(
            ctx.store,
            ctx.store.household_id_or_none(),
            household_checkin_notice(checkin_question(), channel="push"),
            settings=resolved_settings,
            base_url=resolved_settings.oauth_base_url,
            secret=resolved_settings.session_secret,
        )
        ctx.store.mark_checkin_sent()
        return {
            "sent": True,
            "notified": notified,
            "checked_at": now_local.strftime("%Y-%m-%d %H:%M"),
        }

    # -- daily delta digest ---------------------------------------------------

    @router.post("/household/digest", tags=["household"])
    def set_digest(
        payload: DigestConfig,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Turn the opt-in daily delta digest on or off (shared by both parents)."""
        ctx.store.set_digest_enabled(payload.enabled)
        return {"ok": True, "enabled": payload.enabled}

    @router.post("/household/balance", tags=["household"])
    def set_balance(
        payload: BalanceConfig,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Turn the opt-in load-balance view on or off (shared by both parents)."""
        ctx.store.set_balance_enabled(payload.enabled)
        return {"ok": True, "enabled": payload.enabled}

    @router.post("/webhooks/household/digest/check", tags=["household"])
    def digest_check(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Push each parent the other parent's unseen sheet changes (self-suppressing).

        A periodic trigger POSTs here; per member we diff what the *other* parent
        changed since that member last looked (or was last digested), and — if
        there's anything new and it's been ~a day since their last digest — send
        just that member a warm catch-up. Silent when nothing's new; opt-in.
        """
        now = utcnow()
        now_str = now.strftime(TS_FMT)
        # Nothing to balance in a household of one — skip (and it can't have any
        # "other parent" changes anyway).
        if not ctx.store.is_shared_household() or not ctx.store.get_digest_enabled():
            return {"sent": [], "checked_at": now_str}
        from prefrontal.integrations.delivery import (
            deliver_to_member,
            household_digest_notice,
        )

        hid = ctx.store.household_id_or_none()
        sent: list[dict[str, Any]] = []
        for member in ctx.store.household_members(hid):
            if member.get("status") not in (None, "active"):
                continue
            m_store = ctx.store.scoped(member["id"])
            digested = m_store.get_state("household_digested_at")
            since = max(m_store.get_state("household_seen_at") or "", digested or "")
            changes = unseen_changes(m_store, viewer_id=member["id"], since=since)
            if not changes or not digest_interval_ok(digested, now):
                continue
            delivery = deliver_to_member(
                m_store,
                household_digest_notice(digest_message(changes), channel="push"),
                handle=member["handle"],
                settings=resolved_settings,
                base_url=resolved_settings.oauth_base_url,
                secret=resolved_settings.session_secret,
            )
            m_store.set_state("household_digested_at", now_str, source="inferred")
            sent.append(
                {"handle": member["handle"], "count": len(changes), "delivery": delivery}
            )
        return {"sent": sent, "checked_at": now_str}

    # -- shared shopping list -------------------------------------------------

    @router.get("/household/shopping", tags=["household"])
    def list_shopping(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """The shared shopping list on its own (still-needed first).

        A light read for surfaces that only want the list — the dashboard's
        quick-add card — without pulling the whole household sheet. 404s for a
        user in no household, mirroring the write endpoints.
        """
        return {"items": ctx.store.shopping_items()}

    @router.post("/household/shopping", status_code=status.HTTP_201_CREATED, tags=["household"])
    def add_shopping(
        payload: ShoppingAdd,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Add a thing to buy to the shared list (stamps who added it)."""
        item = payload.item.strip()
        if not item:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="item must be non-empty"
            )
        sid = ctx.store.add_shopping_item(
            item=item, spec=payload.spec, where_to_buy=payload.where_to_buy,
            child_id=payload.child_id, added_by=ctx.user["id"],
        )
        return {"id": sid, "item": item}

    @router.post("/household/shopping/{item_id}/got", tags=["household"])
    def shopping_got(
        item_id: int,
        payload: ShoppingGot,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Check a shopping item off (or un-check it), stamping who bought it."""
        if not ctx.store.set_shopping_got(item_id, payload.got, user_id=ctx.user["id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such item")
        return {"ok": True, "got": payload.got}

    @router.post("/household/shopping/clear-got", tags=["household"])
    def clear_got_shopping(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Clear every checked-off ("got it") item at once; still-needed rows stay.

        The bulk sweep after a shop, so a shared list doesn't accumulate a wall of
        done entries. Idempotent — with nothing checked off it clears ``0``.
        """
        return {"cleared": ctx.store.clear_got_shopping_items()}

    @router.post("/household/shopping/{item_id}/remove", tags=["household"])
    def remove_shopping(
        item_id: int,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Delete a shopping item (scoped to the household)."""
        if not ctx.store.remove_shopping_item(item_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such item")
        return {"removed": True}

    # -- recurring shared chores ----------------------------------------------

    def _resolve_member_id(
        ctx: ScopedRequest, uid: int | None, field: str
    ) -> int | None:
        """Validate ``uid`` is an active member (or None), else 422."""
        if uid is None:
            return None
        members = {m["id"] for m in ctx.store.household_members(ctx.store.household_id_or_none())}
        if uid not in members:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} must be a member of this household (or null).",
            )
        return uid

    def _resolve_owner_id(ctx: ScopedRequest, owner_id: int | None) -> int | None:
        """Validate ``owner_id`` is an active member (or None), else 422."""
        return _resolve_member_id(ctx, owner_id, "owner_id")

    @router.post("/household/routines", tags=["household"])
    def set_routine(
        payload: RoutineSet,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Upsert a routine (keyed on title within the household).

        A routine groups chores under one **accountable** owner (the mental-load
        holder) and carries the schedule its chores inherit. Editing re-submits the
        same title; the chores linked under it are untouched.
        """
        clean, error = normalize_routine(payload.model_dump())
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
            )
        clean["accountable_id"] = _resolve_member_id(
            ctx, clean["accountable_id"], "accountable_id"
        )
        rid = ctx.store.set_routine(updated_by=ctx.user["id"], **clean)
        return {"id": rid, "title": clean["title"]}

    @router.post("/household/routines/{routine_id}/enabled", tags=["household"])
    def set_routine_enabled(
        routine_id: int,
        payload: RoutineEnabled,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Pause or resume a routine (and the schedule its chores inherit)."""
        if not ctx.store.set_routine_enabled(routine_id, payload.enabled):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such routine")
        return {"ok": True, "enabled": payload.enabled}

    @router.post("/household/routines/{routine_id}/remove", tags=["household"])
    def remove_routine(
        routine_id: int,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Delete a routine; its chores survive, unlinked (they stand alone)."""
        if not ctx.store.remove_routine(routine_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such routine")
        return {"removed": True}

    @router.post("/household/chores", tags=["household"])
    def set_chore(
        payload: ChoreSet,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Upsert a recurring shared chore (keyed on title within the household).

        The chore's schedule drives the notification flow: a lead-time reminder to
        the owner, and — if it slips past due — a gentle heads-up to the other
        parent. A chore may join a ``routine_id`` (inheriting its schedule) and may
        be untimed. Editing re-submits the same title; the completion log is untouched.
        """
        clean, error = normalize_chore(payload.model_dump())
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
            )
        clean["owner_id"] = _resolve_owner_id(ctx, clean["owner_id"])
        routine_id = payload.routine_id
        if routine_id is not None and ctx.store.routine(routine_id) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="routine_id must be a routine in this household (or null).",
            )
        cid = ctx.store.set_chore(updated_by=ctx.user["id"], routine_id=routine_id, **clean)
        return {"id": cid, "title": clean["title"]}

    @router.post("/household/chores/{chore_id}/enabled", tags=["household"])
    def set_chore_enabled(
        chore_id: int,
        payload: ChoreEnabled,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Pause or resume a chore's reminders without deleting it."""
        if not ctx.store.set_chore_enabled(chore_id, payload.enabled):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chore")
        return {"ok": True, "enabled": payload.enabled}

    @router.post("/household/chores/{chore_id}/done", tags=["household"])
    def mark_chore_done(
        chore_id: int,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Mark a chore done for today (idempotent), attributed to the acting parent."""
        today = local_datetime(utcnow(), resolved_settings.timezone).strftime("%Y-%m-%d")
        result = ctx.store.log_chore_done(
            chore_id=chore_id, done_on=today, done_by=ctx.user["id"]
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chore")
        return {"ok": True, "created": result["created"], "done_on": today}

    @router.post("/household/chores/{chore_id}/remove", tags=["household"])
    def remove_chore(
        chore_id: int,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Delete a chore and its completion log (scoped to the household)."""
        if not ctx.store.remove_chore(chore_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such chore")
        return {"removed": True}

    @router.post("/webhooks/household/chores/check", tags=["household"])
    def chores_check(
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Fire any chore reminders / miss-handoffs due now for the caller's household.

        A periodic trigger (n8n / launchd, like the coach/panic/star-prompt checks)
        POSTs here. The sweep + delivery live in :func:`run_chores_check` so the CLI
        twin shares one code path; each stage dedups to once per local day, so a
        check running every 15–30 min fires each exactly once. Silent when nothing's
        due.
        """
        now = utcnow()
        sent = run_chores_check(ctx.store, settings=resolved_settings, now=now)
        now_local = local_datetime(now, resolved_settings.timezone)
        return {"sent": sent, "checked_at": now_local.strftime("%Y-%m-%d %H:%M")}

    # -- appointments ---------------------------------------------------------

    @router.post(
        "/household/appointments", status_code=status.HTTP_201_CREATED, tags=["household"]
    )
    def add_appointment(
        payload: AppointmentCreate,
        ctx: Annotated[ScopedRequest, Depends(require_member)],
    ) -> dict[str, Any]:
        """Add a kid appointment as a ``kind='child'`` commitment on the caller's calendar.

        The shared sheet surfaces upcoming child appointments across the caller's
        calendar; other co-parents see them from their own view when they sync the
        same event (v1 keeps commitments per-user).
        """
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
