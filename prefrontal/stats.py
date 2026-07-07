"""Behavioral insights — aggregate episodes into the stats the /stats page charts.

The learning loop records raw :mod:`episodes`; this module rolls them into the
three signature views the Insights page renders (all pure and model-free, mirroring
:mod:`prefrontal.briefing` / :mod:`prefrontal.household` — a deterministic
``build_stats`` a template or a test can consume without HTTP):

1. **Time-estimation accuracy** — how your estimates compare to what actually
   happened: the overall bias multiplier ("you run ~1.4× over your estimates")
   and the same per context, from episodes carrying both a predicted and an
   actual value.
2. **Follow-through** — outcomes over time: the success rate, the current streak,
   the success/miss/partial split, and a recent chronological series for a
   sparkline.
3. **Channel responsiveness** — the acknowledgement rate per delivery channel
   ("which channel you actually answer"), from episodes that recorded a channel
   and whether you responded.
4. **Self-care** — per basic-needs check (meal, water, meds): how often you
   genuinely acted on the nudge vs. snoozed/ignored it, the average nudge→tap
   latency on the taps you confirmed, and the learned cadence vs. its default.

Everything is derived straight from ``episodes`` (not the ``patterns`` table), so
the page is meaningful even before ``prefrontal learn`` has ever run.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from prefrontal.commitments import resolve_zone
from prefrontal.config import get_settings
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import CHECKS, SELF_CARE_EPISODE

#: Outcome values we treat as terminal for the follow-through view, in display order.
OUTCOMES: tuple[str, ...] = ("success", "partial", "miss")

#: How many recent outcomes the follow-through sparkline shows.
FOLLOW_SERIES_LEN = 24

#: How many estimate points the scatter/summary keeps (most recent).
MAX_ESTIMATE_POINTS = 60


def _ratio_summary(pairs: list[float]) -> dict[str, Any]:
    """Median over/under ratio for a list of actual/predicted ratios (or empty)."""
    if not pairs:
        return {"n": 0, "ratio": None, "direction": None}
    ratio = median(pairs)
    direction = "over" if ratio > 1.05 else "under" if ratio < 0.95 else "on"
    return {"n": len(pairs), "ratio": round(ratio, 2), "direction": direction}


def _time_estimation(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate-vs-actual bias overall, per context, plus recent points to plot."""
    overall: list[float] = []
    by_ctx: dict[str, list[float]] = {}
    points: list[dict[str, Any]] = []
    for e in episodes:
        # `switch` episodes store impulse counts in predicted/actual, not
        # duration estimates, so they must not enter time-estimation accuracy.
        if e.get("episode_type") == "switch":
            continue
        pred, act = e.get("predicted_value"), e.get("actual_value")
        if pred is None or act is None or pred <= 0 or act < 0:
            continue
        ratio = act / pred
        overall.append(ratio)
        by_ctx.setdefault(e.get("episode_type") or "other", []).append(ratio)
        points.append(
            {"predicted": round(pred, 2), "actual": round(act, 2),
             "context": e.get("episode_type") or "other"}
        )
    contexts = [
        {"context": ctx, **_ratio_summary(vals)}
        for ctx, vals in sorted(by_ctx.items(), key=lambda kv: -len(kv[1]))
    ]
    return {
        **_ratio_summary(overall),
        "contexts": contexts,
        "points": points[-MAX_ESTIMATE_POINTS:],
    }


def _follow_through(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome counts, success rate, current success streak, and a recent series."""
    outs = [e.get("outcome") for e in episodes if e.get("outcome") in OUTCOMES]
    counts = {o: outs.count(o) for o in OUTCOMES}
    total = len(outs)
    rate = round(counts["success"] / total, 2) if total else None
    # Current streak: trailing consecutive successes (episodes are chronological asc).
    streak = 0
    for o in reversed(outs):
        if o == "success":
            streak += 1
        else:
            break
    return {"n": total, "counts": counts, "rate": rate, "streak": streak,
            "series": outs[-FOLLOW_SERIES_LEN:]}


def _channels(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Acknowledgement rate per delivery channel (most-used first)."""
    agg: dict[str, dict[str, int]] = {}
    for e in episodes:
        ch = e.get("channel")
        ack = e.get("acknowledged")
        if not ch or ack is None:
            continue
        row = agg.setdefault(ch, {"n": 0, "acked": 0})
        row["n"] += 1
        row["acked"] += 1 if ack else 0
    out = [
        {"channel": ch, "n": r["n"], "acked": r["acked"],
         "rate": round(r["acked"] / r["n"], 2) if r["n"] else None}
        for ch, r in agg.items()
    ]
    out.sort(key=lambda c: -c["n"])
    return out


#: Self-care outcomes we tally per check, in display order.
SELF_CARE_OUTCOMES: tuple[str, ...] = ("confirmed", "snoozed", "ignored")


def _self_care_latency(notes: str | None) -> float | None:
    """Parse ``latency=<n>s`` from an episode's notes, or ``None`` if absent.

    A tiny local re-implementation of the self-care module's own latency parse
    (private there), so this stays a leaf that only reads public episode fields.
    """
    if not notes:
        return None
    m = re.search(r"latency=(\d+)s", notes)
    return float(m.group(1)) if m else None


def _active_local_days(episodes: list[dict[str, Any]], tz: ZoneInfo) -> int:
    """Count the distinct local calendar days on which any episode occurred.

    Episode timestamps are stored UTC-naive; we read them in the deployment's
    home zone so a late-evening tap counts toward the right local day (not the
    next UTC one). This is the denominator for the typical-day average.
    """
    days: set[Any] = set()
    for e in episodes:
        raw = e.get("timestamp")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days.add(dt.astimezone(tz).date())
    return len(days)


def _self_care(store: MemoryStore) -> list[dict[str, Any]]:
    """Per basic-needs check: response split, act-on-it rate, latency, and cadence.

    One row per :data:`~prefrontal.modules.self_care.CHECKS` entry (stable order
    meal, water, meds). Each ``self_care`` episode's ``context`` is
    ``"<key>: confirmed|snoozed|ignored"`` and its ``outcome`` echoes that verb;
    confirmed taps carry a ``latency=<n>s`` note. Safe/zeroed on empty history.
    """
    episodes = store.episodes_by_type(SELF_CARE_EPISODE, limit=10_000)
    # A check is "on" only when both the master switch and its own toggle are on
    # (mirrors self_care_status). The Insights card shows every on check, greying
    # the ones that have no responses yet, so `enabled` travels with each row.
    master_on = (store.get_state("self_care", "off") or "off") == "on"
    tz = resolve_zone(None, get_settings().timezone)
    rows: list[dict[str, Any]] = []
    for check in CHECKS:
        prefix = f"{check.key}: "
        mine = [e for e in episodes if str(e.get("context") or "").startswith(prefix)]
        counts = {o: sum(1 for e in mine if e.get("outcome") == o) for o in SELF_CARE_OUTCOMES}
        # `n` is every episode for the check; anything whose outcome isn't one of
        # the three known verbs is "unknown" (unaccounted). Kept for completeness
        # (the Insights bar now greys the daily-target shortfall, not this).
        n = len(mine)
        unknown = n - sum(counts.values())
        # Typical-day adherence: average confirms per active local day vs the
        # daily target, so the Insights bar fills green toward the goal and greys
        # the shortfall — rather than normalising to responses received.
        target = max(1, int(store.get_float(check.target_key, check.target_default)))
        active_days = _active_local_days(mine, tz)
        avg_per_day = round(counts["confirmed"] / active_days, 1) if active_days else 0.0
        latencies = [
            lat
            for e in mine
            if e.get("outcome") == "confirmed"
            and (lat := _self_care_latency(e.get("notes"))) is not None
        ]
        rows.append(
            {
                "key": check.key,
                "enabled": master_on
                and (store.get_state(check.enabled_key, "on") or "on") == "on",
                "n": n,
                "confirmed": counts["confirmed"],
                "snoozed": counts["snoozed"],
                "ignored": counts["ignored"],
                "unknown": unknown,
                "target": target,
                "avg_per_day": avg_per_day,
                "response_rate": round(counts["confirmed"] / n, 2) if n else None,
                "avg_latency_seconds": round(fmean(latencies), 1) if latencies else None,
                "interval_minutes": max(
                    1, int(store.get_float(check.interval_key, check.interval_default))
                ),
                "default_interval_minutes": check.interval_default,
            }
        )
    return rows


def build_stats(store: MemoryStore) -> dict[str, Any]:
    """Assemble the Insights payload from the (scoped) user's episodes.

    Args:
        store: A user-scoped :class:`MemoryStore`.

    Returns:
        ``{time_estimation, follow_through, channels, self_care, counts}`` — see
        the module docstring. All fields are JSON-serializable and safe on an
        empty history (counts are 0, ratios/rates are ``None``).
    """
    episodes = store.all_episodes()
    return {
        "time_estimation": _time_estimation(episodes),
        "follow_through": _follow_through(episodes),
        "channels": _channels(episodes),
        "self_care": _self_care(store),
        "counts": {"episodes": len(episodes)},
    }
