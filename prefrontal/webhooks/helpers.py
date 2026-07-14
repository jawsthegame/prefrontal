"""Small formatting and URL helpers shared across the webhook routers.

Speakable one-line read-backs for the shortcut endpoints, the per-user delivery
routing block returned to n8n, the signed one-tap dismiss link / ntfy action
buttons, the dismiss confirmation page, and the todo-decomposition helper.
Extracted from ``_common`` (and re-exported there during the split).
"""

from __future__ import annotations

import html
from typing import Any

from prefrontal.config import Settings
from prefrontal.memory.store import MemoryStore
from prefrontal.todos import (
    DEFAULT_MAX_FIRST_STEP_MINUTES,
    decompose_task,
    learned_decomposition_guidance,
)
from prefrontal.webhooks.notify import nudge_actions
from prefrontal.webhooks.oauth import sign_dismiss


def _fmt_minutes(value: float | None) -> str:
    """Render a minutes value without a trailing ``.0`` (30.0 -> "30", 12.5 -> "12.5")."""
    if value is None:
        return "?"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


#: Window sources that mean the user stated the duration; anything else was
#: guessed by the server (and the confirmation says so, so the user can correct).
_EXACT_WINDOW_SOURCES = {"explicit", "parsed"}


def _outing_started_confirmation(
    intention: str, minutes: float, source: str, domain: str | None = None
) -> str:
    """One-line, speakable read-back for a started outing — flags a guessed window.

    When a life-domain is known at declaration (given, or inferred from the
    intention), it's named so the user sees the outing arrived pre-filed — and can
    correct it with ``/webhooks/outing/domain`` if the guess is off.
    """
    mins = _fmt_minutes(minutes)
    filed = f" Filed under {domain}." if domain else ""
    if source in _EXACT_WINDOW_SOURCES:
        return f"Tracking “{intention}” for {mins} min — I'll nudge you to head back.{filed}"
    if source == "history":
        return (
            f"Tracking “{intention}” for ~{mins} min — your usual for this. Say "
            f"“back in N min” to set it exactly. I'll nudge you to head back.{filed}"
        )
    return (
        f"Tracking “{intention}” for ~{mins} min (estimated — say “back in N min” "
        f"to set it exactly). I'll nudge you to head back.{filed}"
    )


def _outing_return_confirmation(
    status: str, actual: float | None, window: float | None, outcome: str
) -> str:
    """One-line read-back for a closed outing."""
    if status != "returned":
        return "Outing closed (abandoned) — no worries, logged it."
    out = _fmt_minutes(actual)
    planned = _fmt_minutes(window)
    verdict = "on time 👍" if outcome == "success" else f"over the {planned} min you planned"
    return f"Welcome back — out {out} min, {verdict}."


def _focus_started_confirmation(task: str, minutes: float | None, aligned: bool) -> str:
    """One-line read-back for a started focus session."""
    bits = [f"Focus on “{task}” started"]
    if minutes is not None:
        bits.append(f"planned {_fmt_minutes(minutes)} min")
    bits.append("protected from nudges" if aligned else "not flagged as your intended task")
    return " — ".join(bits) + "."


def _focus_end_confirmation(status: str, actual: float | None, planned: float | None) -> str:
    """One-line read-back for a closed focus session."""
    if status != "ended":
        return "Focus session closed (abandoned) — logged it."
    out = _fmt_minutes(actual)
    if planned is not None:
        return f"Focus ended — {out} min on it (planned {_fmt_minutes(planned)})."
    return f"Focus ended — {out} min on it."


def _impulse_captured_confirmation(title: str) -> str:
    """One-line read-back for a captured-and-deferred impulse."""
    return f"Parked “{title}” — it's safe in your list. Back to what you were doing."


def _switch_resolved_confirmation(action: str, task: str, title: str | None) -> str:
    """One-line read-back for how a switch-impulse was resolved."""
    if action == "return":
        return f"Good call — back to “{task}.”"
    if action == "defer":
        return f"Parked “{title}” for later — staying on “{task}.”"
    return f"Switched off “{task}.” Logged it — go do the new thing."


#: Per-user delivery routing keys read from coaching state (the destination for a
#: nudge; the signing/account creds stay global). ``apns_token`` is the product
#: push target; ``ntfy_topic`` feeds the dev-only shim. See docs/multi-tenant.md §6.5.
_DELIVERY_KEYS = ("apns_token", "twilio_to", "twilio_from", "ntfy_topic")


def _delivery_fields(memory: MemoryStore) -> dict[str, Any]:
    """Return ``{delivery: {...}, delivery_configured: bool}`` for the scoped user.

    n8n reads ``delivery`` to route a nudge to this user's target instead of a
    hardcoded credential; ``delivery_configured`` is ``False`` when none is set
    (so the dashboard can warn and n8n can fall back to the operator default).
    """
    delivery = {k: memory.get_state(k) for k in _DELIVERY_KEYS}
    return {
        "delivery": delivery,
        "delivery_configured": any(v for v in delivery.values()),
    }


def _dismiss_url(
    settings: Settings, handle: str, kind: str, target_id: int
) -> str:
    """Build a signed one-tap dismiss link for a nudge, or ``""`` if unavailable.

    Returns ``""`` unless both a public origin (``oauth_base_url``) and a signing
    key (``session_secret``) are configured — the link is opened from the phone
    off-box, so it needs the Tailscale HTTPS origin, and it must be signed so a
    tap can't be forged. n8n drops it into the Pushover ``url`` field.
    """
    if not settings.oauth_base_url or not settings.session_secret:
        return ""
    token = sign_dismiss(handle, kind, target_id, settings.session_secret)
    return f"{settings.oauth_base_url}/nudge/dismiss?t={token}"


def _nudge_actions(
    settings: Settings, handle: str, kind: str, target_id: int | None
) -> list[dict[str, Any]]:
    """Signed ntfy one-tap action buttons for a nudge (``[]`` when unconfigured).

    A thin settings-aware wrapper over
    :func:`prefrontal.webhooks.notify.nudge_actions`, so routers can attach an
    ``actions`` list to a nudge response for a "publish to ntfy" delivery node.
    """
    return nudge_actions(
        kind,
        target_id,
        base_url=settings.oauth_base_url,
        secret=settings.session_secret,
        handle=handle,
    )


def _dismiss_page(headline: str) -> str:
    """A tiny self-contained confirmation page shown after a one-tap dismiss."""
    safe = html.escape(headline)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Prefrontal</title></head>"
        "<body style='font-family:-apple-system,system-ui,sans-serif;"
        "display:flex;min-height:90vh;align-items:center;justify-content:center;"
        "text-align:center;color:#222;margin:0;padding:1.5rem'>"
        f"<div><div style='font-size:2.5rem'>✓</div><p style='font-size:1.15rem'>{safe}</p>"
        "<p style='color:#888;font-size:.9rem'>You can close this page.</p></div>"
        "</body></html>"
    )


def _decompose_and_store(
    memory: MemoryStore, todo_id: int, title: str, client: Any
) -> dict[str, Any]:
    """Generate a todo's first-step decomposition, persist it, and return it."""
    max_first = memory.get_float(
        "max_first_step_minutes", DEFAULT_MAX_FIRST_STEP_MINUTES
    )
    d = decompose_task(
        title,
        max_first_minutes=max_first,
        client=client,
        guidance=learned_decomposition_guidance(memory),
    )
    memory.set_decomposition(
        todo_id,
        first_step=d.first_step,
        first_step_minutes=d.first_step_minutes,
        steps=d.steps,
        source=d.source,
    )
    return {
        "first_step": d.first_step,
        "first_step_minutes": d.first_step_minutes,
        "steps": d.steps,
        "source": d.source,
    }




#: Per-request timeout (seconds) for the hot-path window inference. Kept short:
#: ``/outing/start`` is interactive (an iOS Shortcut waits on it), so a slow or
#: unreachable model must degrade to the heuristic fast rather than hang the tap.
