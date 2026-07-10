"""HTTP routes tagged "admin".

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

from prefrontal.memory.store import (
    provision_user,
)
from prefrontal.selfupdate import run_restart, run_update
from prefrontal.webhooks.deps import (
    ScopedRequest,
    require_operator,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    HouseholdCreate,
    HouseholdMember,
    UserCreate,
    UserEmail,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "admin" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings

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
        if payload.email and unscoped.get_user_by_email(payload.email) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email '{payload.email}' is already used by another user.",
            )
        user, token = provision_user(
            unscoped,
            payload.handle,
            display_name=payload.display_name,
            is_operator=payload.is_operator,
            email=payload.email,
        )
        return {
            "handle": user["handle"],
            "display_name": user["display_name"],
            "is_operator": bool(user["is_operator"]),
            "email": user["email"],
            "token": token,  # shown once
        }

    @router.post("/admin/users/{handle}/email", tags=["admin"])
    def admin_set_user_email(
        handle: str,
        payload: UserEmail,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Set (or, with a blank email, clear) the user's Google sign-in address.

        This is what makes a *new* user reachable by Google sign-in: the callback
        resolves the verified email to a user from the DB, so no
        ``GOOGLE_OAUTH_ALLOWED`` env edit + restart is needed. The email must be
        unique across users (a 409 otherwise).
        """
        store = request.app.state.store
        try:
            changed = store.set_user_email(handle, payload.email)
        except ValueError as exc:  # email already claimed by another user
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from None
        if not changed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
            )
        return {"handle": handle, "email": store.get_user(handle)["email"]}

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

    @router.post("/admin/users/{handle}/enable", tags=["admin"])
    def admin_enable_user(
        handle: str,
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Re-enable a disabled user (their token resolves again). Idempotent.

        The inverse of :func:`admin_disable_user`, so a mistaken disable is
        recoverable from the admin UI rather than only over the CLI.
        """
        if not request.app.state.store.set_user_status(handle, "active"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
            )
        return {"handle": handle, "status": "active"}

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

    @router.get("/admin/households", tags=["admin"])
    def admin_list_households(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """List households and their members (operator view), for the admin UI."""
        return {"households": request.app.state.store.list_households()}

    @router.get("/admin/whoami", tags=["admin"])
    def admin_whoami(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The signed-in user's admin-relevant capabilities, for the UI to gate on.

        Any authenticated user may call it (a non-operator just sees
        ``is_operator: false``). The dashboard uses it to decide whether to show
        the operator-only Update / Restart controls.
        """
        return {
            "handle": ctx.user["handle"],
            "is_operator": bool(ctx.user.get("is_operator")),
            "self_update_enabled": resolved_settings.self_update_enabled,
        }

    # -- remote update / restart (operator-only, opt-in) -----------------------

    def _require_self_update() -> None:
        """403 unless remote self-update is explicitly enabled (it runs code)."""
        if not resolved_settings.self_update_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Remote update is off. Set PREFRONTAL_SELF_UPDATE=on to enable it.",
            )

    @router.post("/admin/update", tags=["admin"])
    def admin_update(
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Pull the latest code, reinstall + migrate, then restart the service.

        Operator-only and gated by ``PREFRONTAL_SELF_UPDATE`` — it runs whatever
        is on the deployed branch. The update runs synchronously (its output is
        returned); on success the restart is spawned detached so this response
        flushes before the process is bounced. A failed update skips the restart.
        """
        _require_self_update()
        return run_update(resolved_settings, restart=True)

    @router.post("/admin/restart", tags=["admin"])
    def admin_restart(
        ctx: Annotated[ScopedRequest, Depends(require_operator)],
    ) -> dict[str, Any]:
        """Restart the service without updating (detached, so this response flushes)."""
        _require_self_update()
        return run_restart(resolved_settings)

    return router
