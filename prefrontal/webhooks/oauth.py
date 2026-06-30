"""Google sign-in for the web surfaces (dashboard / family view).

This is *browser* login only. The automated clients — n8n, iOS Shortcuts, the
Scriptable widget — stay on per-user ``X-Prefrontal-Token`` tokens, because they
can't do an interactive OAuth redirect. So this layers a signed session cookie
beside the token auth: a human signs in with Google once, the cookie carries them
after that, and :func:`prefrontal.webhooks.app.resolve_user` accepts either.

Flow: ``/auth/google/login`` → Google consent → ``/auth/google/callback`` (verify
the email against the ``GOOGLE_OAUTH_ALLOWED`` allowlist, map it to a user, set
the cookie) → back to ``/dashboard``. Only allow-listed emails can sign in.

No new dependency: the code-for-email exchange uses ``httpx`` (already vendored),
and the session cookie is a small HMAC-signed token (no JWT library).
"""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from prefrontal.config import Settings

SESSION_COOKIE = "prefrontal_session"
STATE_COOKIE = "pf_oauth_state"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(handle: str, secret: str, *, now: float | None = None) -> str:
    """Return a signed session token for ``handle`` (``msg.signature``)."""
    exp = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    msg = f"{_b64(handle.encode())}.{exp}"
    sig = hmac.new(secret.encode(), msg.encode(), sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_session(cookie: str, secret: str, *, now: float | None = None) -> str | None:
    """Return the handle from a valid, unexpired session cookie, else ``None``."""
    if not cookie or not secret:
        return None
    try:
        b_handle, exp_str, sig = cookie.split(".")
        msg = f"{b_handle}.{exp_str}"
        expected = hmac.new(secret.encode(), msg.encode(), sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(exp_str) < (now if now is not None else time.time()):
            return None
        return _unb64(b_handle).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def session_user(request: Request) -> dict[str, Any] | None:
    """Resolve a request's session cookie to an active user row, or ``None``.

    Used by ``resolve_user`` as the browser-login path, alongside the token header.
    """
    settings: Settings = request.app.state.settings
    if not settings.session_secret:
        return None
    handle = verify_session(request.cookies.get(SESSION_COOKIE, ""), settings.session_secret)
    if not handle:
        return None
    row = request.app.state.store.get_user(handle)
    return row if row is not None and row["status"] == "active" else None


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.oauth_base_url}/auth/google/callback"


def _exchange_code_for_email(
    code: str, settings: Settings, *, client: httpx.Client | None = None
) -> str | None:
    """Trade an auth code for the signed-in Google account's verified email."""
    owns = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        tok = client.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": _redirect_uri(settings),
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            return None
        access = tok.json().get("access_token")
        if not access:
            return None
        info = client.get(_USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access}"})
        if info.status_code != 200:
            return None
        data = info.json()
        if not data.get("email_verified"):
            return None
        email = data.get("email")
        return email.strip().lower() if isinstance(email, str) else None
    except httpx.HTTPError:
        return None
    finally:
        if owns:
            client.close()


def register_oauth_routes(app: FastAPI, settings: Settings) -> None:
    """Mount the Google sign-in routes onto ``app`` (no-op endpoints if disabled)."""
    secure = settings.oauth_base_url.startswith("https://")

    @app.get("/auth/google/login", tags=["auth"])
    def google_login() -> RedirectResponse:
        if not settings.google_oauth_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not configured.")
        state = secrets.token_urlsafe(24)
        params = urlencode(
            {
                "client_id": settings.google_oauth_client_id,
                "redirect_uri": _redirect_uri(settings),
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        resp = RedirectResponse(f"{_AUTH_ENDPOINT}?{params}")
        resp.set_cookie(
            STATE_COOKIE, state, max_age=600, httponly=True, secure=secure, samesite="lax"
        )
        return resp

    @app.get("/auth/google/callback", tags=["auth"])
    def google_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        if not settings.google_oauth_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not configured.")
        expected_state = request.cookies.get(STATE_COOKIE, "")
        if not state or not expected_state or not hmac.compare_digest(state, expected_state):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state.")
        if not code:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing authorization code.")

        email = _exchange_code_for_email(code, settings)
        handle = settings.oauth_allowed_emails.get(email or "")
        if not handle:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "That Google account isn't allowed to sign in here.",
            )
        user = request.app.state.store.get_user(handle)
        if user is None or user["status"] != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"User '{handle}' is not active.")

        resp = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            SESSION_COOKIE,
            sign_session(handle, settings.session_secret),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        resp.delete_cookie(STATE_COOKIE)
        return resp

    @app.post("/auth/logout", tags=["auth"])
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp
