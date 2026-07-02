"""Interactive notification actions — one-tap ntfy buttons for a nudge.

Pushover's only interactivity is a single "open this URL" link, so acting on a
nudge means leaving the app you're in. `ntfy <https://ntfy.sh>`_ notifications
support inline **action buttons** — including ``http`` actions that fire a
request *in the background*, with no app switch — which makes genuinely one-tap
nudge responses possible: **Wrap up** ends a focus session, **I'm back** closes
an outing, **Made it / Missed it** logs a commitment outcome, all without
opening anything.

Each button targets the signed ``GET /nudge/act`` endpoint (see
:func:`prefrontal.webhooks.oauth.sign_action`), the same self-authenticating,
self-expiring link mechanism the one-tap *dismiss* already uses: a bare GET
carrying a signed ``handle|action|target_id`` token, so a background tap needs
no ``X-Prefrontal-Token`` header and can't be forged.

This module is pure — it only builds URL strings and ntfy action dicts. The
publish itself is done by whatever delivery path is configured (today an n8n
"publish to ntfy" node reads the ``actions`` a nudge endpoint returns).
"""

from __future__ import annotations

from typing import Any

from prefrontal.webhooks.oauth import sign_action

#: The buttons each nudge kind offers, as ``(action, label)`` in tap order. The
#: ``action`` must be one of :data:`prefrontal.webhooks.oauth.NUDGE_ACTIONS`.
_NUDGE_BUTTONS: dict[str, tuple[tuple[str, str], ...]] = {
    "focus": (("focus_end", "Wrap up"),),
    "outing": (("outing_return", "I'm back"), ("outing_abandon", "Abandon")),
    "departure": (("made_it", "Made it"), ("missed_it", "Missed it")),
    # Reflective pause — resolve the pull to switch in one tap (was a second
    # /focus/resolve call plus a multi-tap menu).
    "pause": (
        ("switch_return", "Stay on task"),
        ("switch_defer", "Park it"),
        ("switch_switch", "Switch anyway"),
    ),
    # Overwhelm nudge — a one-tap confirmation that the surfaced first step got
    # done, so the drift pass learns whether the panic first-step lever works.
    "panic": (("panic_step_done", "✓ Did it"),),
    # Self-care meal check — confirm you've eaten (silences it for the day) or
    # ask again in a bit.
    "meal": (("meal_ate", "✓ Ate"), ("meal_snooze", "Snooze")),
}


def act_url(
    base_url: str, handle: str, action: str, target_id: int, secret: str
) -> str:
    """Build a signed one-tap ``/nudge/act`` URL, or ``""`` when unconfigured.

    Returns ``""`` unless both a public origin (``oauth_base_url``) and a signing
    key (``session_secret``) are set — the link is opened from the phone off-box,
    so it needs the Tailscale HTTPS origin and must be signed.
    """
    if not base_url or not secret:
        return ""
    token = sign_action(handle, action, target_id, secret)
    return f"{base_url}/nudge/act?t={token}"


def _http_button(label: str, url: str) -> dict[str, Any]:
    """An ntfy ``http`` action button that fires a background GET on tap.

    ``clear: true`` dismisses the notification once the tap succeeds, so a
    handled nudge doesn't linger.
    """
    return {"action": "http", "label": label, "url": url, "method": "GET", "clear": True}


def _view_button(label: str, url: str) -> dict[str, Any]:
    """An ntfy ``view`` action button that opens ``url`` on tap.

    Unlike :func:`_http_button` this fires no background request — it just opens
    the URL (a web page), so it needs no signing. ``clear: false`` keeps the
    notification around after opening, since viewing isn't resolving.
    """
    return {"action": "view", "label": label, "url": url, "clear": False}


def panic_actions(base_url: str, *, label: str = "Open triage") -> list[dict[str, Any]]:
    """Return the ntfy action button for a proactive panic nudge, or ``[]``.

    A single ``view`` button that opens the full panic triage in the dashboard
    (the ``?panic=1`` deep link auto-opens the overlay), so a proactive overwhelm
    nudge — which already carries the first step inline — is one tap away from the
    whole picture. Empty when no public origin is configured; ``view`` needs no
    signing since it only opens a page (the page itself is auth-gated).

    Args:
        base_url: Public HTTPS origin (``settings.oauth_base_url``).
        label: Button label (defaults to "Open triage").
    """
    if not base_url:
        return []
    return [_view_button(label, f"{base_url}/dashboard?panic=1")]


def nudge_actions(
    kind: str,
    target_id: int | None,
    *,
    base_url: str,
    secret: str,
    handle: str,
) -> list[dict[str, Any]]:
    """Return the ntfy action buttons for a nudge, or ``[]`` when unavailable.

    Empty when the nudge kind has no buttons, the target is unknown, or signing /
    the public origin isn't configured — so a caller can always attach the result
    unconditionally and get no buttons rather than broken ones.

    Args:
        kind: a key of :data:`_NUDGE_BUTTONS` (``"focus"`` / ``"outing"`` /
            ``"departure"`` / ``"pause"`` / ``"panic"`` / ``"meal"``).
        target_id: The focus-session / outing / commitment / meal id the buttons
            act on (for ``meal`` a synthetic date-int; the tap acts on "today").
        base_url: Public HTTPS origin (``settings.oauth_base_url``).
        secret: Signing key (``settings.session_secret``).
        handle: The acting user's handle (embedded in the signed token).

    Returns:
        A list of ntfy action-button dicts (at most a few), in tap order.
    """
    if target_id is None or not base_url or not secret:
        return []
    buttons: list[dict[str, Any]] = []
    for action, label in _NUDGE_BUTTONS.get(kind, ()):  # unknown kind → no buttons
        url = act_url(base_url, handle, action, target_id, secret)
        if url:
            buttons.append(_http_button(label, url))
    return buttons
