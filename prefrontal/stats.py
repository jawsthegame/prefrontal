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

Everything is derived straight from ``episodes`` (not the ``patterns`` table), so
the page is meaningful even before ``prefrontal learn`` has ever run.
"""
from __future__ import annotations

from statistics import median
from typing import Any

from prefrontal.memory.store import MemoryStore

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


def build_stats(store: MemoryStore) -> dict[str, Any]:
    """Assemble the Insights payload from the (scoped) user's episodes.

    Args:
        store: A user-scoped :class:`MemoryStore`.

    Returns:
        ``{time_estimation, follow_through, channels, counts}`` — see the module
        docstring. All fields are JSON-serializable and safe on an empty history
        (counts are 0, ratios/rates are ``None``).
    """
    episodes = store.all_episodes()
    return {
        "time_estimation": _time_estimation(episodes),
        "follow_through": _follow_through(episodes),
        "channels": _channels(episodes),
        "counts": {"episodes": len(episodes)},
    }
