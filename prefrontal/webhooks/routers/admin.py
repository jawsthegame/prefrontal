"""HTTP routes tagged "admin".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from prefrontal.webhooks._common import (
    Annotated,
    Any,
    Depends,
    HouseholdCreate,
    HouseholdMember,
    HTTPException,
    ScopedRequest,
    UserCreate,
    provision_user,
    require_operator,
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
    """Build the "admin" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post("/admin/users", status_code=status.HTTP_201_CREATED, tags=["admin"])
    def admin_create_user(
        payload: UserCreate,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Provision a new user, returning the raw token **once**.

        Thin HTTP wrapper over :func:`prefrontal.memory.store.provision_user`
        (the same path the CLI uses), so an operator can add a user over
        Tailscale. The token is shown here and never again — store it now.
        """
        unscoped = request.app.state.store  # user CRUD lives on the unscoped store
        if unscoped.get_user(payload.handle) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Handle '{payload.handle}' is already taken.",
            )
        user, token = provision_user(
            unscoped,
            payload.handle,
            display_name=payload.display_name,
            is_operator=payload.is_operator,
        )
        return {
            "handle": user["handle"],
            "display_name": user["display_name"],
            "is_operator": bool(user["is_operator"]),
            "token": token,  # shown once
        }

    @router.get("/admin/users", tags=["admin"])
    def admin_list_users(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """List users (handles, display names, status) — never their tokens."""
        return {"users": request.app.state.store.list_users()}

    @router.post("/admin/users/{handle}/rotate", tags=["admin"])
    def admin_rotate_user(
        handle: str,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Mint a new token for ``handle`` (old devices stop working). Shown once."""
        token = request.app.state.store.rotate_user_token(handle)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
            )
        return {"handle": handle, "token": token}

    @router.post("/admin/users/{handle}/disable", tags=["admin"])
    def admin_disable_user(
        handle: str,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Disable a user (their token stops resolving). Idempotent."""
        if not request.app.state.store.set_user_status(handle, "disabled"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
            )
        return {"handle": handle, "status": "disabled"}

    # -- household membership (operator-set in v1; see docs/household-sheet.md §8)

    @router.post(
        "/admin/households", status_code=status.HTTP_201_CREATED, tags=["admin"]
    )
    def admin_create_household(
        payload: HouseholdCreate,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Create a household, returning its id. Wire members in with the next route."""
        hid = request.app.state.store.create_household(payload.name)
        return {"id": hid, "name": payload.name}

    @router.post("/admin/households/{household_id}/members", tags=["admin"])
    def admin_add_household_member(
        household_id: int,
        payload: HouseholdMember,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Put a user into ``household_id`` (both co-parents share its rows)."""
        store = request.app.state.store
        try:
            changed = store.set_user_household(payload.handle, household_id)
        except ValueError as exc:  # unknown household id
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from None
        if not changed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
            )
        return {
            "household_id": household_id,
            "members": store.household_members(household_id),
        }

    return router
