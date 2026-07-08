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
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
STATE_TTL_SECONDS = 600  # 10 min — the OAuth round-trip should be well under this

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(value: str, secret: str, ttl: int, *, now: float | None = None) -> str:
    """Return ``b64(value).exp.hmac`` — a signed, self-expiring token."""
    exp = int((now if now is not None else time.time()) + ttl)
    msg = f"{_b64(value.encode())}.{exp}"
    sig = hmac.new(secret.encode(), msg.encode(), sha256).hexdigest()
    return f"{msg}.{sig}"


def _verify(token: str, secret: str, *, now: float | None = None) -> str | None:
    """Return the signed value from a valid, unexpired token, else ``None``."""
    if not token or not secret:
        return None
    try:
        b_value, exp_str, sig = token.split(".")
        expected = hmac.new(secret.encode(), f"{b_value}.{exp_str}".encode(), sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(exp_str) < (now if now is not None else time.time()):
            return None
        return _unb64(b_value).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def sign_session(handle: str, secret: str, *, now: float | None = None) -> str:
    """Signed browser-session token carrying the user's handle."""
    return _sign(handle, secret, SESSION_TTL_SECONDS, now=now)


def verify_session(cookie: str, secret: str, *, now: float | None = None) -> str | None:
    """Return the handle from a valid session cookie, else ``None``."""
    return _verify(cookie, secret, now=now)


def sign_state(secret: str, *, now: float | None = None) -> str:
    """A short-lived signed CSRF state — no cookie needed, so it survives the
    cross-site redirect back from Google (Safari/strict cookie policies)."""
    return _sign(secrets.token_urlsafe(12), secret, STATE_TTL_SECONDS, now=now)


def verify_state(state: str, secret: str, *, now: float | None = None) -> bool:
    """Whether a state token is validly signed and unexpired."""
    return _verify(state, secret, now=now) is not None


#: How long a one-tap "dismiss this nudge" link stays valid. A nudge is only
#: actionable for a short while (an active coffee outing, an imminent departure),
#: so the link self-expires — it lives in the phone's notification history, and a
#: short TTL bounds how long a leaked notification could silence a future alert.
DISMISS_TTL_SECONDS = 12 * 3600

#: Nudge kinds a dismiss link may target. Kept explicit so a forged/garbled
#: ``kind`` can never reach a handler.
_DISMISS_KINDS = ("outing", "departure")


def sign_dismiss(
    handle: str, kind: str, target_id: int, secret: str, *, now: float | None = None
) -> str:
    """Signed, self-expiring token authorizing a nudge dismissal.

    The token carries the user ``handle`` so the ``GET /nudge/dismiss`` endpoint
    can resolve the acting user without an ``X-Prefrontal-Token`` header — a
    Pushover notification tap opens a bare browser GET that sends no header.
    ``|`` is a safe separator because the value is base64-encoded inside
    :func:`_sign`.
    """
    return _sign(f"{handle}|{kind}|{target_id}", secret, DISMISS_TTL_SECONDS, now=now)


def verify_dismiss(
    token: str, secret: str, *, now: float | None = None
) -> tuple[str, str, int] | None:
    """Return ``(handle, kind, target_id)`` from a valid dismiss token, else ``None``.

    ``None`` covers every failure — bad signature, expiry, unknown ``kind``, or a
    non-integer id — so callers treat any malformed link as simply invalid.
    """
    raw = _verify(token, secret, now=now)
    if raw is None:
        return None
    try:
        handle, kind, target_id = raw.split("|")
    except ValueError:
        return None
    if kind not in _DISMISS_KINDS:
        return None
    try:
        return handle, kind, int(target_id)
    except ValueError:
        return None


#: Interactive nudge actions a one-tap ntfy button may invoke — the "act on this
#: nudge without opening the app" set, distinct from the passive ``dismiss``.
#: Kept explicit so a forged/garbled action can never reach a handler.
NUDGE_ACTIONS = (
    "focus_end",       # "Wrap up" — end an active focus session
    "outing_return",   # "I'm back" — close an outing as returned
    "outing_abandon",  # "Abandon" — close an outing as abandoned
    "made_it",         # made a commitment on time
    "missed_it",       # missed / ran late for a commitment
    "switch_return",   # reflective pause → stay on the current focus block
    "switch_defer",    # reflective pause → park the pull as a todo, stay put
    "switch_switch",   # reflective pause → switch away (close block as switched)
    "panic_step_done", # overwhelm nudge → the surfaced first step got done
    "meal_ate",        # self-care meal check → confirmed eaten today
    "meal_snooze",     # self-care meal check → ask again in a bit
    "water_drank",     # self-care water check → drank (defer a full interval)
    "water_snooze",    # self-care water check → remind again shortly
    "meds_took",       # self-care meds check → took the dose today
    "meds_snooze",     # self-care meds check → ask again in a bit
    "biobreak_went",   # self-care bio-break check → got up (defer a full interval)
    "biobreak_snooze", # self-care bio-break check → remind again shortly
    "winddown_started", # self-care wind-down check → heading to bed (settles the night)
    "winddown_snooze",  # self-care wind-down check → remind again shortly
    "movement_stretched", # self-care movement check → moved/stretched today (settles the day)
    "movement_snooze",    # self-care movement check → ask again in a bit
    "star_award",      # star-chart prompt → award a star for the child today
    "star_skip",       # star-chart prompt → no star today
    "load_light",      # weekly load check-in → felt light this week
    "load_balanced",   # weekly load check-in → felt balanced this week
    "load_heavy",      # weekly load check-in → carried a lot this week
    "digest_seen",     # daily delta digest → mark the sheet seen (all caught up)
    "briefing_helped",     # morning briefing → 👍 it helped (steers the voice)
    "briefing_not_helped", # morning briefing → 👎 it didn't (tighten it up)
    "chore_done",      # shared chore → mark it done for today (whoever tapped)
    "away_confirm",    # multi-day-absence proposal → mark me away (chores reassign)
    "trip_domain_shop",      # trip-label ask → file this trip under shop
    "trip_domain_work",      # trip-label ask → file this trip under work
    "trip_domain_home",      # trip-label ask → file this trip under home
    "trip_domain_kids",      # trip-label ask → file this trip under kids
    "trip_domain_personal",  # trip-label ask → file this trip under personal
)

#: One-tap action links share the dismiss TTL — a nudge is only actionable for a
#: short while, and a short life bounds a leaked notification's blast radius.
ACTION_TTL_SECONDS = DISMISS_TTL_SECONDS


def sign_action(
    handle: str, action: str, target_id: int, secret: str, *, now: float | None = None
) -> str:
    """Signed, self-expiring token authorizing a one-tap nudge action.

    Like :func:`sign_dismiss` but for the *interactive* actions an ntfy button
    fires (wrap up, I'm back, made it …). Carries the ``handle`` so
    ``GET /nudge/act`` resolves the acting user with no ``X-Prefrontal-Token``
    header — a notification tap is a bare background GET. ``|`` is safe because
    the value is base64-encoded inside :func:`_sign`.
    """
    return _sign(f"{handle}|{action}|{target_id}", secret, ACTION_TTL_SECONDS, now=now)


def verify_action(
    token: str, secret: str, *, now: float | None = None
) -> tuple[str, str, int] | None:
    """Return ``(handle, action, target_id)`` from a valid action token, else ``None``.

    ``None`` covers every failure — bad signature, expiry, an action outside
    :data:`NUDGE_ACTIONS`, or a non-integer id — so a malformed link is simply
    inert rather than dangerous.
    """
    raw = _verify(token, secret, now=now)
    if raw is None:
        return None
    try:
        handle, action, target_id = raw.split("|")
    except ValueError:
        return None
    if action not in NUDGE_ACTIONS:
        return None
    try:
        return handle, action, int(target_id)
    except ValueError:
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
        params = urlencode(
            {
                "client_id": settings.google_oauth_client_id,
                "redirect_uri": _redirect_uri(settings),
                "response_type": "code",
                "scope": "openid email",
                # Signed, self-expiring state — verified by signature on the way
                # back, so no cross-site cookie has to survive the Google redirect.
                "state": sign_state(settings.session_secret),
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return RedirectResponse(f"{_AUTH_ENDPOINT}?{params}")

    @app.get("/auth/google/callback", tags=["auth"])
    def google_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        if not settings.google_oauth_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not configured.")
        if not verify_state(state, settings.session_secret):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired sign-in state.")
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
        return resp

    @app.post("/auth/logout", tags=["auth"])
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp
