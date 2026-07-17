"""Request dependencies and identity for the webhook layer.

The FastAPI dependencies every router shares: authentication (resolve the
``X-Prefrontal-Token`` header — or a Google sign-in session cookie — to a user,
matching its ``sha256`` against the ``users`` table) and the scoped
:class:`ScopedRequest` they receive. ``PREFRONTAL_DEFAULT_USER`` lets tokenless
requests resolve to one user (single-user / trusted-LAN mode); the legacy
``PREFRONTAL_WEBHOOK_SECRET`` still works as a bootstrap operator token.

Extracted from ``_common`` so the auth surface is its own module (and re-exported
there for the routers during the split).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from prefrontal.config import Settings
from prefrontal.memory.store import MemoryStore, sha256_hex
from prefrontal.webhooks.oauth import session_user


@dataclass(frozen=True)
class ScopedRequest:
    """The resolved identity + per-user store for an authenticated request.

    Produced by the :func:`resolve_user` dependency. ``store`` is already scoped
    to ``user`` (it injects ``user["id"]`` into every statement), so a handler
    cannot accidentally read or write another user's rows.
    """

    user: dict[str, Any]
    store: MemoryStore


def _resolve_user_row(
    request: Request, token: str | None
) -> dict[str, Any]:
    """Resolve an ``X-Prefrontal-Token`` value to an active user row.

    Resolution order:

    1. A blank/absent token resolves to ``PREFRONTAL_DEFAULT_USER`` when one is
       configured (the single-user / trusted-LAN compatibility mode); without a
       default user a token is required.
    2. A token whose ``sha256`` matches an active user resolves to that user.
    3. The legacy ``PREFRONTAL_WEBHOOK_SECRET`` resolves to the first operator
       user — a bootstrap so an operator can provision the first real tokens.

    Raises:
        HTTPException: 401 if no active user can be resolved.
    """
    store: MemoryStore = request.app.state.store
    settings: Settings = request.app.state.settings

    if not token:
        # Browser surfaces (dashboard/family) carry a Google sign-in session
        # cookie instead of a token header.
        cookie_user = session_user(request)
        if cookie_user is not None:
            return cookie_user
        if settings.default_user:
            row = store.get_user(settings.default_user)
            if row is not None and row["status"] == "active":
                return row
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Prefrontal-Token header.",
        )

    row = store.get_user_by_token_hash(sha256_hex(token))
    if row is not None and row["status"] == "active":
        return row

    # Bootstrap: the legacy shared secret maps to the first operator user, so a
    # fresh deployment can authenticate before any per-user token is minted.
    if settings.webhook_secret and hmac.compare_digest(token, settings.webhook_secret):
        for candidate in store.list_users():
            if candidate["is_operator"] and candidate["status"] == "active":
                return store.get_user(candidate["handle"])

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-Prefrontal-Token header.",
    )


def resolve_user(
    request: Request,
    x_prefrontal_token: Annotated[str | None, Header()] = None,
) -> ScopedRequest:
    """FastAPI dependency: resolve the request's token to a scoped store + user.

    Replaces the old shared-secret check on every data endpoint. Returns a
    :class:`ScopedRequest` carrying the user row and a store already scoped to
    that user, so handlers neither see another user's data nor have to remember
    a ``WHERE user_id = ?``.
    """
    user = _resolve_user_row(request, x_prefrontal_token)
    return ScopedRequest(user=user, store=request.app.state.store.scoped(user["id"]))


def require_operator(
    ctx: Annotated[ScopedRequest, Depends(resolve_user)],
) -> ScopedRequest:
    """Like :func:`resolve_user` but also requires the user be an operator (403)."""
    if not ctx.user.get("is_operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required.",
        )
    return ctx


def require_member(
    ctx: Annotated[ScopedRequest, Depends(resolve_user)],
) -> ScopedRequest:
    """Like :func:`resolve_user` but also requires the caller be in a household.

    A caller in no household has nothing shared to touch, so household routes
    404 rather than surfacing the store's raw scope error. Declared as a
    dependency (not a per-handler call) so it attaches via ``Depends`` and a new
    household endpoint can't forget the guard.
    """
    if ctx.store.household_id_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You're not set up in a household.",
        )
    return ctx
