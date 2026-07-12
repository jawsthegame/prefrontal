"""Weekly feature-usage nudge — the "act on it" half of the usage loop.

The `/stats` Insights page (:mod:`prefrontal.stats`) surfaces which push features
*fire but get ignored*. This module closes that loop: once a week, if a coaching
module keeps nudging you and you rarely act on it, Prefrontal sends **one** gentle
push offering a one-tap **Mute** (silence that module entirely) or **Keep** (leave
it on, and don't ask again for a while). insight → suggestion → one-tap change.

Pure-core + a thin delivery entry point, mirroring :mod:`prefrontal.panic` /
:mod:`prefrontal.household`:

- :func:`build_usage_nudge` — deterministic pick of the worst firing-but-ignored,
  non-muted, non-snoozed module from the usage rollup (or ``None``).
- :func:`run_usage_check` — the weekly sweep: dedup to once per ISO week, deliver
  the push with the Mute/Keep buttons, and stash the *pending* target the one-tap
  handler resolves. Behind ``POST /webhooks/usage/check`` and ``prefrontal usage``.

Only **modules** (things the coaching tick fans over, keyed by module key) are
ever suggested for muting — a pull surface like panic/briefing is opened on
demand, so "you're not using it" is not something to mute. Muting is applied by
the tick's per-user filter (see :func:`prefrontal.coaching.run_coaching_tick`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from prefrontal.clock import fmt_ts, local_datetime, parse_ts, utcnow
from prefrontal.coaching import Cue, Decision
from prefrontal.config import Settings, get_settings
from prefrontal.integrations.delivery import (
    DeliveryClient,
    resolve_route,
)
from prefrontal.log import get_logger
from prefrontal.modules import enabled_modules
from prefrontal.modules.registry import get as get_module
from prefrontal.stats import (
    USAGE_IGNORED_MIN_OFFERED,
    USAGE_IGNORED_RATE,
    USAGE_WINDOW_DAYS,
)
from prefrontal.webhooks.notify import act_url

logger = get_logger(__name__)

#: ISO week (``"2026-W27"``) the weekly nudge last went out — the once-a-week
#: dedup, per user, in ``coaching_state``.
NUDGE_WEEK_KEY = "usage_nudge_week"

#: The feature the live nudge's one-tap buttons act on. Set when the nudge is
#: delivered; read by the ``usage_mute``/``usage_keep`` handlers (which carry a
#: synthetic target of 0, like the self-care checks, since the module key can't
#: ride the int-only signed target).
NUDGE_PENDING_KEY = "usage_nudge_pending"

#: How long a "Keep" tap snoozes re-suggesting that same feature, so a deliberate
#: keep isn't re-asked next week.
KEEP_SNOOZE_DAYS = 56


def _kept_key(feature: str) -> str:
    """coaching_state key stamping when a feature was last "kept" (snooze cursor)."""
    return f"usage_kept:{feature}"


def week_key(dt: datetime) -> str:
    """ISO week key (``"2026-W27"``) for once-per-week dedup."""
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _recently_kept(store: Any, feature: str, now: datetime) -> bool:
    """Whether ``feature`` was "kept" within :data:`KEEP_SNOOZE_DAYS`."""
    seen = parse_ts(store.get_state(_kept_key(feature)))
    return seen is not None and (now - seen).days < KEEP_SNOOZE_DAYS


def build_usage_nudge(
    store: Any, settings: Settings | None = None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Pick the worst firing-but-ignored coaching module to suggest muting.

    A candidate is an **enabled module** (not a pull surface) that, over the usage
    window, fired at least :data:`~prefrontal.stats.USAGE_IGNORED_MIN_OFFERED`
    times with an engagement rate below
    :data:`~prefrontal.stats.USAGE_IGNORED_RATE`, is not already muted, and hasn't
    been "kept" recently. The worst (lowest rate, then most-fired) wins.

    Returns:
        A ``{feature, title, offered, engaged, rate, message}`` dict, or ``None``
        when nothing is worth nudging about.
    """
    now = now or utcnow()
    module_keys = {m.key for m in enabled_modules(settings)}
    muted = store.muted_features()
    best: dict[str, Any] | None = None
    for row in store.feature_usage_rollup(USAGE_WINDOW_DAYS):
        feature = row["feature"]
        if feature not in module_keys or feature in muted:
            continue
        offered = int(row.get("offered") or 0)
        engaged = int(row.get("engaged") or 0)
        if offered < USAGE_IGNORED_MIN_OFFERED:
            continue
        rate = engaged / offered
        if rate >= USAGE_IGNORED_RATE or _recently_kept(store, feature, now):
            continue
        cand = {"feature": feature, "offered": offered, "engaged": engaged, "rate": rate}
        # Worst offender: lowest engagement rate, then most-fired.
        if best is None or (rate, -offered) < (best["rate"], -best["offered"]):
            best = cand
    if best is None:
        return None
    best["rate"] = round(best["rate"], 2)
    best["title"] = _feature_title(best["feature"])
    best["message"] = (
        f"“{best['title']}” has nudged you {best['offered']}× in the last "
        f"{USAGE_WINDOW_DAYS} days and you've acted on {best['engaged']}. "
        "Want to mute it, or keep it?"
    )
    return best


def _feature_title(feature: str) -> str:
    """Human title for a module key, falling back to a prettified key."""
    try:
        return get_module(feature).title or feature.replace("_", " ").title()
    except KeyError:
        return feature.replace("_", " ").title()


def _usage_buttons(handle: str, settings: Settings) -> list[dict[str, Any]]:
    """The Mute / Keep one-tap buttons for the weekly nudge (empty if unsigned).

    Both act on the *pending* feature stashed at delivery — the module key can't
    ride the int-only signed target, so the buttons carry a synthetic 0.
    """
    base, secret = settings.oauth_base_url, settings.session_secret
    buttons: list[dict[str, Any]] = []
    for action, label in (("usage_mute", "🔕 Mute"), ("usage_keep", "Keep")):
        url = act_url(base, handle, action, 0, secret)
        if url:
            buttons.append(
                {"action": "http", "label": label, "url": url, "method": "GET", "clear": True}
            )
    return buttons


def apply_usage_decision(store: Any, action: str, *, now: datetime | None = None) -> str:
    """Apply a one-tap ``usage_mute`` / ``usage_keep`` on the pending feature.

    The tapped nudge named no id (the module key can't ride the int-only signed
    target), so both act on :data:`NUDGE_PENDING_KEY` — the feature stashed when
    the nudge went out. Idempotent: once the pending target is cleared, a second
    tap gets a friendly "already handled" line rather than muting nothing.

    Args:
        store: A store scoped to the tapping user.
        action: ``"usage_mute"`` or ``"usage_keep"``.
        now: Optional naive-UTC "now" (for the keep-snooze stamp).

    Returns:
        A one-line confirmation to push back and render.
    """
    now = now or utcnow()
    feature = store.get_state(NUDGE_PENDING_KEY)
    if not feature:
        return "That suggestion's already been handled. 🙂"
    store.set_state(NUDGE_PENDING_KEY, "", source="inferred")
    title = _feature_title(feature)
    if action == "usage_mute":
        store.set_feature_muted(feature, True)
        return (
            f"Muted {title} — I'll stop nudging you about it. "
            "(Turn it back on anytime in Settings.)"
        )
    # usage_keep — leave it on, and don't re-suggest it for a while.
    store.set_state(_kept_key(feature), fmt_ts(now), source="inferred")
    return f"Kept {title} on. 👍 I won't ask again for a while."


def run_usage_check(
    store: Any,
    *,
    settings: Settings | None = None,
    handle: str = "",
    now: datetime | None = None,
    client: DeliveryClient | None = None,
    deliver: bool = True,
) -> dict[str, Any]:
    """Run one weekly usage check for a (scoped) user; deliver at most one nudge.

    Once per ISO week at most: if a module is firing-but-ignored, send the push
    with Mute/Keep buttons and stash the pending target the buttons resolve. The
    week is stamped only after a delivery attempt, so a quiet week (nothing to
    nudge) doesn't burn the slot.

    Args:
        store: A store scoped to the user being checked.
        settings: Operator settings (timezone + signing origin/secret).
        handle: The user's handle, for signing the one-tap links.
        now: Optional naive-UTC "now".
        client: Optional delivery client (tests inject one).
        deliver: When ``False``, a side-effect-free dry run — report what *would*
            be sent without stashing a pending target or stamping the week.

    Returns:
        A small report dict: ``{delivered, feature?, offered?, engaged?, reason?}``.
    """
    resolved = settings or get_settings()
    now = now or utcnow()
    week = week_key(local_datetime(now, resolved.timezone))
    if store.get_state(NUDGE_WEEK_KEY) == week:
        return {"delivered": False, "reason": "already nudged this week"}

    nudge = build_usage_nudge(store, resolved, now=now)
    if nudge is None:
        return {"delivered": False, "reason": "no firing-but-ignored feature"}

    report: dict[str, Any] = {
        "delivered": False,
        "feature": nudge["feature"],
        "offered": nudge["offered"],
        "engaged": nudge["engaged"],
    }
    if not deliver:
        # Dry run (e.g. `usage check` preview): report only, no mutation — don't
        # stash a pending target or burn the weekly slot the real sweep needs.
        return report

    # Stash the pending target *before* delivery so the one-tap buttons resolve it.
    store.set_state(NUDGE_PENDING_KEY, nudge["feature"], source="inferred")
    route = resolve_route(store, resolved)
    client = client or DeliveryClient.from_settings(resolved)
    cue = Cue(
        module="usage",
        intervention="weekly_nudge",
        urgency="ambient",
        text=nudge["message"],
        context_key="usage",  # unmapped → no auto buttons; extra_actions wins
        dedup_key="usage_nudge",
    )
    result = client.deliver(
        Decision(cue=cue, channel="push", text=nudge["message"]),
        route,
        base_url=resolved.oauth_base_url,
        secret=resolved.session_secret,
        handle=handle,
        extra_actions=_usage_buttons(handle, resolved),
    )
    report["delivered"] = result.delivered
    report["transport"] = result.transport
    # Stamp the week only after a real delivery attempt, so a nudge-worthy week
    # isn't skipped — and a dry-run preview never consumes the slot.
    store.set_state(NUDGE_WEEK_KEY, week, source="inferred")
    return report
