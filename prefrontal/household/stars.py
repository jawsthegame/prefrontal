"""Star charts — the reward-system half of household agreements.

Extracted from the ``household`` god-module (audit #408): parsing an agreement's
``structured`` JSON, the earn-only star-tier / reward-goal maths (thresholds,
"newly reached", "next goal"), the congratulation copy, and
``award_stars_and_notify`` (grant + dual-parent congratulation). Self-contained;
re-exported from :mod:`prefrontal.household` so existing imports are unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from prefrontal.memory.store import MemoryStore

# --- star chart goals (pure) -------------------------------------------------
#
# An agreement's `structured` JSON declares the reward goals; the running total
# lives in the household_stars ledger. These helpers turn (structured, totals)
# into the reached/next-goal facts the award endpoint and the render both need —
# kept pure so the "did this grant cross a reward line?" decision is unit-tested
# without a store, a transport, or a request.


def parse_structured(raw: Any) -> Any:
    """Parse an agreement's ``structured`` JSON string, or ``None`` if absent/bad."""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _star_thresholds(structured: Any) -> list[tuple[int, str]]:
    """Sorted ``(count, reward)`` goal pairs from a chart's ``structured`` JSON.

    Tolerates the template's shapes: the count lives under the chart's ``unit``
    key, or ``stars``/``points``. Malformed entries are skipped, not raised on.
    """
    if not isinstance(structured, dict):
        return []
    raw = structured.get("thresholds")
    if not isinstance(raw, list):
        return []
    unit = structured.get("unit", "star")
    out: list[tuple[int, str]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if unit in t:
            n = t.get(unit)
        elif "stars" in t:
            n = t.get("stars")
        else:
            n = t.get("points")
        reward = t.get("reward")
        if isinstance(n, (int, float)) and reward:
            out.append((int(n), str(reward)))
    out.sort(key=lambda pair: pair[0])
    return out


_TIER_RE = re.compile(r"^\s*(\d+)\s*=\s*(.+?)\s*$")


def parse_star_tiers(raw: str) -> list[dict[str, Any]] | None:
    """Parse a ``"7=small LEGO, 30=large"`` reward-tier spec into thresholds.

    The server-side twin of the ``/kids`` field's parser: each comma-separated
    ``count=reward`` becomes ``{"stars": count, "reward": reward}``, sorted by
    count. Blanks/garbage are skipped; ``None`` when nothing valid parses (so the
    caller can reject rather than store an empty chart). Multiple tiers are the
    point — e.g. a small reward at 7 and a bigger one at 30.
    """
    tiers: list[dict[str, Any]] = []
    for part in (raw or "").split(","):
        m = _TIER_RE.match(part)
        if m:
            tiers.append({"stars": int(m.group(1)), "reward": m.group(2).strip()})
    tiers.sort(key=lambda t: t["stars"])
    return tiers or None


def _unit_of(structured: Any) -> str:
    """The chart's counting unit (``"star"`` by default)."""
    return structured.get("unit", "star") if isinstance(structured, dict) else "star"


def newly_reached_goals(structured: Any, before: int, after: int) -> list[dict[str, Any]]:
    """Goals whose threshold the total crossed going ``before`` → ``after``.

    A goal ``n`` is newly reached when ``before < n <= after`` — so re-awarding
    at or above a threshold never re-fires it, and a single big grant that leaps
    past several thresholds reports each one (each is its own reward to hand out).
    """
    unit = _unit_of(structured)
    return [
        {"count": n, "unit": unit, "reward": reward}
        for n, reward in _star_thresholds(structured)
        if before < n <= after
    ]


def next_goal(structured: Any, total: int) -> dict[str, Any] | None:
    """The nearest not-yet-reached goal above ``total`` (with ``remaining``), or ``None``."""
    for n, reward in _star_thresholds(structured):
        if n > total:
            return {
                "count": n,
                "unit": _unit_of(structured),
                "reward": reward,
                "remaining": n - total,
            }
    return None


def star_congrats_text(child_name: str | None, goal: dict[str, Any]) -> str:
    """The celebration line sent to both parents when a reward goal is hit."""
    count = goal["count"]
    unit = goal["unit"]
    label = unit if count == 1 else f"{unit}s"
    who = child_name or "The kids"
    return f"🌟 {who} hit {count} {label} — reward unlocked: {goal['reward']}!"


def _resolve_child_name(store: MemoryStore, child_id: int) -> str | None:
    """The roster name for ``child_id`` (``None`` for household-wide id 0).

    Searches the whole roster — kids and pets — since a fact/agreement can hang
    off either (they share the ``children.id`` space, so an id matches at most one
    row).
    """
    if not child_id:
        return None
    roster = store.children() + store.pets()
    return next((m["name"] for m in roster if m["id"] == child_id), None)


def award_stars_and_notify(
    store: MemoryStore,
    agreement_id: int,
    *,
    delta: int,
    awarded_by: int | None,
    note: str | None = None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Award stars, detect crossed reward goals, and congratulate + notify both parents.

    The single code path behind every way a star can be given — the HTTP endpoint,
    the CLI, the one-tap notification button, and (via the caller) a scheduled
    prompt — so the "did this cross a goal? then tell both parents" rule lives in
    exactly one place.

    Returns ``None`` if the agreement isn't in the store's household (so a caller
    can 404). Raises :class:`ValueError` when a negative ``delta`` is applied to an
    earn-only chart. On success returns the new ``total``, the ``goals_reached``,
    the ``next_goal``, the resolved ``child_name``, and the per-parent ``notified``
    records (empty unless a goal was crossed).
    """
    agreement = store.agreement(agreement_id)
    if agreement is None:
        return None
    structured = parse_structured(agreement.get("structured"))
    if isinstance(structured, dict) and structured.get("earn_only") and delta < 0:
        raise ValueError("this chart is earn-only — stars can't be taken away")
    result = store.award_stars(
        agreement_id=agreement_id, delta=delta, awarded_by=awarded_by, note=note
    )
    assert result is not None  # agreement existence already confirmed above
    goals = newly_reached_goals(structured, result["before"], result["after"])
    child_name = _resolve_child_name(store, agreement.get("child_id") or 0)
    notified: list[dict[str, Any]] = []
    if goals:
        # Lazy import: delivery pulls in webhooks.notify, which would re-enter the
        # partially-initialized webhooks package if imported at module load.
        from prefrontal.delivery import (
            deliver_to_household,
            household_notice,
        )

        message = " ".join(star_congrats_text(child_name, g) for g in goals)
        notified = deliver_to_household(
            store,
            store.household_id_or_none(),
            household_notice(message, channel="sound"),
            settings=settings,
            client=client,
        )
    return {
        "agreement": agreement,
        "child_name": child_name,
        "total": result["after"],
        "delta": result["delta"],
        "goals_reached": goals,
        "next_goal": next_goal(structured, result["after"]),
        "notified": notified,
    }


