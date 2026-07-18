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
from urllib.parse import quote

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
    # Self-care water check — confirm a drink (defers a full interval) or snooze.
    "water": (("water_drank", "✓ Drank"), ("water_snooze", "Snooze")),
    # Self-care meds check — confirm the dose (silences it for the day) or snooze.
    "meds": (("meds_took", "✓ Took"), ("meds_snooze", "Snooze")),
    # Self-care bio-break check — confirm you got up (defers a full interval) or snooze.
    "biobreak": (("biobreak_went", "✓ Went"), ("biobreak_snooze", "Snooze")),
    # Self-care wind-down check — confirm you're heading to bed (settles it for the
    # night) or snooze a little.
    "winddown": (("winddown_started", "🌙 Winding down"), ("winddown_snooze", "Snooze")),
    # Self-care movement check — confirm you moved/stretched (settles it for the
    # day) or snooze a little.
    "movement": (("movement_stretched", "🧘 Stretched"), ("movement_snooze", "Snooze")),
    # Star-chart award prompt — award a star in one tap, or skip for today.
    "star": (("star_award", "⭐ Yes"), ("star_skip", "Not today")),
    # Weekly mental-load check-in — a gentle self-report (ntfy caps buttons at 3).
    "load": (
        ("load_light", "Felt light 🙂"),
        ("load_balanced", "Balanced ⚖️"),
        ("load_heavy", "Carried a lot 🫠"),
    ),
    # Daily delta digest — acknowledge you've seen the other parent's changes.
    "digest": (("digest_seen", "Caught up 👍"),),
    # Recurring shared chore — mark it done for today in one tap (whoever taps
    # gets the credit, so a partner picking up a slipped chore closes the loop).
    "chore": (("chore_done", "✓ Done"),),
    # Multi-day-absence proposal — one tap marks the member away, so their chores
    # reassign to the present co-parent for the trip (target rides the trip id).
    "away": (("away_confirm", "✅ Mark me away"),),
    # Vacation entry suggestion — one tap eases off the non-urgent nudges until you
    # return (target rides the trip id; ignoring it is a no-op, never a re-nag).
    "vacation": (("vacation_confirm", "🏝️ Ease off"),),
    # Trip check-in — while a parent is out, one tap posts a status to the other
    # co-parent (ntfy caps at 3). Target rides the trip id; the tap relays a notice.
    "trip_checkin": (
        ("trip_status_homeward", "🏠 Heading home"),
        ("trip_status_late", "⏰ Running late"),
        ("trip_status_ok", "👍 All good"),
    ),
    # Closed-loop trip label ask — the one-tap "file into a life-sphere" buttons
    # are built per-user from the configured quick-file domains (ntfy caps at 3),
    # not from this static map. See :func:`trip_label_actions`.
}

#: Life-domain → one-tap button label (emoji + short word) for the trip-label ask.
#: Every canonical domain has one, so a user can surface any ≤3 of them (a
#: shopkeeper wants 🛒 Shop) — not just the default home/kids/personal trio.
DOMAIN_BUTTON_LABELS: dict[str, str] = {
    "shop": "🛒 Shop",
    "work": "💼 Work",
    "home": "🏠 Home",
    "kids": "🧒 Kids",
    "personal": "🙋 Me",
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


#: Default name of the iOS Shortcut the evening morning-prep nudge deep-links to.
#: The user creates a Shortcut with this name that takes the wake time as text
#: input and sets an alarm; renaming it means setting ``alarm_shortcut_name``.
DEFAULT_ALARM_SHORTCUT = "Set Alarm"


def alarm_actions(shortcut_name: str, wake_hhmm: str) -> list[dict[str, Any]]:
    """The ntfy "⏰ Set alarm" button for the evening morning-prep nudge, or ``[]``.

    A client-side ``view`` action (no server round-trip, so no signing) that opens
    the iOS **Shortcuts** URL scheme: ``shortcuts://run-shortcut?name=<name>&input=
    text&text=<HH:MM>``. The user makes a Shortcut of that name which reads the
    passed time and creates an alarm, so the evening heads-up is one tap from an
    alarm actually being set — closing the gap between "worth setting an alarm" and
    doing it. Empty when the shortcut name or wake time is missing (a plain push).

    Args:
        shortcut_name: The iOS Shortcut to run (``alarm_shortcut_name``, default
            :data:`DEFAULT_ALARM_SHORTCUT`).
        wake_hhmm: Suggested wake time as ``HH:MM``, passed as the shortcut's input.
    """
    if not shortcut_name or not wake_hhmm:
        return []
    url = f"shortcuts://run-shortcut?name={quote(shortcut_name)}&input=text&text={quote(wake_hhmm)}"
    return [_view_button("⏰ Set alarm", url)]


def alarm_actions_for_cue(cue: Any) -> list[dict[str, Any]]:
    """Build the morning-prep alarm button from a cue's ``ref``, or ``[]``.

    The Time Blindness module stamps ``ref['alarm_at']`` (suggested wake HH:MM) and
    ``ref['alarm_shortcut']`` (the Shortcut name) when it emits a ``morning_prep``
    cue, so both delivery paths (the native client and the n8n ``/coach/check``
    fan-out) can attach the same button off the cue without re-reading state.
    """
    ref = cue.ref or {}
    return alarm_actions(ref.get("alarm_shortcut", ""), ref.get("alarm_at", ""))


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


def trip_label_actions(
    domains: list[str] | None,
    trip_id: int | None,
    *,
    base_url: str,
    secret: str,
    handle: str,
) -> list[dict[str, Any]]:
    """One-tap "file this trip" buttons for the label ask — one per configured domain.

    The per-user analog of :func:`nudge_actions` for the trip-label ask: instead of
    a hard-coded home/kids/personal trio, it builds a ``trip_domain_<d>`` button for
    each domain in ``domains`` (the user's :func:`~prefrontal.focus_balance.resolve_quick_domains`
    choice, ntfy-capped at 3), labeled from :data:`DOMAIN_BUTTON_LABELS`. Falls back
    to the default trio when ``domains`` is empty, and returns ``[]`` when the target
    or signing/origin is missing (so a caller can attach it unconditionally).
    """
    if trip_id is None or not base_url or not secret:
        return []
    picks = domains or ["home", "kids", "personal"]
    buttons: list[dict[str, Any]] = []
    for domain in picks[:3]:  # ntfy caps action buttons at 3
        label = DOMAIN_BUTTON_LABELS.get(domain, domain.title())
        url = act_url(base_url, handle, f"trip_domain_{domain}", trip_id, secret)
        if url:
            buttons.append(_http_button(label, url))
    return buttons
