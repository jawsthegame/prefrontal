"""Behavioral insights — aggregate episodes into the stats the /stats page charts.

The learning loop records raw :mod:`episodes`; this module rolls them into the
three signature views the Insights page renders (all pure and model-free, mirroring
:mod:`prefrontal.briefing` / :mod:`prefrontal.household` — a deterministic
``build_stats`` a template or a test can consume without HTTP):

1. **Time-estimation accuracy** — how your estimates compare to what actually
   happened: the overall bias multiplier ("you run ~1.4× over your estimates")
   and the same per context, from episodes carrying both a predicted and an
   actual value.
2. **Follow-through** — outcomes over time: the completion rate, a *forgiving*
   momentum read (recent rate, its trend, whether you've returned after a lapse,
   and a personal-best stretch — deliberately **not** a consecutive-success
   streak, which turns any miss into a shame trigger; see ``_follow_through``),
   the success/miss/partial split, and a recent chronological series for a
   sparkline.
3. **Channel responsiveness** — the acknowledgement rate per delivery channel
   ("which channel you actually answer"), from episodes that recorded a channel
   and whether you responded.
4. **Self-care** — per basic-needs check (meal, water, meds): how often you
   genuinely acted on the nudge vs. snoozed/ignored it, the average nudge→tap
   latency on the taps you confirmed, and the learned cadence vs. its default.
5. **Chores** — shared-chore completions over the last month: the total, a
   per-person split, today/yesterday counts, and a recent day-by-day series —
   read from the ``household_chore_log`` rather than ``episodes``.
6. **Feature usage** — which features you lean on, which fire-and-get-ignored,
   and which lie dormant, from the ``feature_events`` stream joined against the
   module registry (so a never-fired feature reads as dormant, not absent); each
   flagged with whether it's currently muted. The "act on it" half of this view
   is the weekly usage nudge in :mod:`prefrontal.usage`.

The first four are derived straight from ``episodes`` (not the ``patterns``
table), so the page is meaningful even before ``prefrontal learn`` has ever run;
the chores view reads the household completion log, and feature usage the
``feature_events`` stream.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from prefrontal.clock import local_datetime, parse_ts, utcnow
from prefrontal.commitments import resolve_zone
from prefrontal.config import get_settings
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import CHECKS, SELF_CARE_EPISODE

#: Outcome values we treat as terminal for the follow-through view, in display order.
OUTCOMES: tuple[str, ...] = ("success", "partial", "miss")

#: How many recent outcomes the follow-through sparkline shows.
FOLLOW_SERIES_LEN = 24

#: Rolling window (most-recent N outcomes) for the follow-through momentum signals
#: — the recent completion rate, its trend, and the personal-best stretch.
#: Count-based (not calendar) so it's meaningful before there's much history.
FOLLOW_RECENT_WINDOW = 10

#: Trend needs at least this many outcomes in *each* of the recent and prior
#: windows before it will name a direction (else ``trend`` stays ``None``).
FOLLOW_TREND_MIN = 3

#: A recent-vs-prior completion-rate change smaller than this reads as "steady".
FOLLOW_TREND_DELTA = 0.1

#: A gap of at least this many days between logged outcomes counts as a lapse you
#: stepped away from; returning after one is the signal this app celebrates
#: ("design for the return, not the streak") — in place of a fragile success
#: streak, whose loss is the classic "what-the-hell" abandonment trigger.
FOLLOW_LAPSE_GAP_DAYS = 4

#: How many estimate points the scatter/summary keeps (most recent).
MAX_ESTIMATE_POINTS = 60

#: Rolling window (days) for the Insights chores view.
CHORE_WINDOW_DAYS = 30

#: How many recent days the chores completion sparkline shows (oldest→newest).
CHORE_SERIES_LEN = 14

#: Rolling window (days) for the feature-usage view.
USAGE_WINDOW_DAYS = 30

#: A push feature must have fired at least this many times before a low
#: engagement rate counts as "firing but ignored" — below it there isn't enough
#: signal to call it noise rather than just new.
USAGE_IGNORED_MIN_OFFERED = 3

#: Engagement rate (engaged / offered) below which a push feature that has fired
#: enough times is flagged "firing but ignored" — a candidate to retune or mute.
USAGE_IGNORED_RATE = 0.15

#: The pull surfaces (things you open / invoke, not module nudges) the usage view
#: measures, so one you've never opened still shows up as dormant rather than
#: silently missing. Names match :data:`prefrontal.webhooks.usage.PULL_SURFACES`
#: and the CLI pull map.
PULL_FEATURES: tuple[str, ...] = (
    "dashboard", "stats", "panic", "briefing", "balance", "profile",
    "encouragement", "clarify", "calendar", "review", "household", "projects",
    "self_care", "scheduling", "modules", "trip_tracking",
)


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
    """Outcome split, completion rate, and *forgiving* momentum signals.

    Deliberately **not** a consecutive-success streak: a trailing-success count
    turns any miss into a "you lost it" moment, which the ADHD abandonment
    research flags as the classic trigger of the "what-the-hell" spiral (see
    ``docs/encouragement.md`` and the roadmap's "design for the return, not the
    streak"). Instead we surface recovery- and direction-oriented signals a single
    miss can't zero out:

    - ``rate`` — overall completion rate (the split, not a loss count);
    - ``recent_rate`` — completion rate over the last :data:`FOLLOW_RECENT_WINDOW`
      outcomes ("lately"), which a stray miss only nudges;
    - ``trend`` — ``up``/``steady``/``down`` (recent vs. the prior window), or
      ``None`` without enough history — momentum, not a fragile count;
    - ``returned`` — whether, within that recent window, you stepped away for a
      :data:`FOLLOW_LAPSE_GAP_DAYS`-day-plus lapse and *came back*: the comeback
      the app celebrates in place of an unbroken streak (it surfaces exactly when
      a streak would have broken);
    - ``best_rate`` — your best :data:`FOLLOW_RECENT_WINDOW`-length stretch ever,
      a personal best that can never be "lost".

    Episodes are chronological asc (:meth:`all_episodes`); a missing/unparseable
    timestamp just can't form a gap.
    """
    rows = [
        (parse_ts(e.get("timestamp")), e["outcome"])
        for e in episodes
        if e.get("outcome") in OUTCOMES
    ]
    outs = [o for _ts, o in rows]
    tally = Counter(outs)
    counts = {o: tally[o] for o in OUTCOMES}  # every OUTCOME present, zero when unseen
    total = len(outs)
    rate = round(counts["success"] / total, 2) if total else None

    def _win_rate(window: list[str]) -> float | None:
        return round(window.count("success") / len(window), 2) if window else None

    recent = outs[-FOLLOW_RECENT_WINDOW:]
    prior = outs[-2 * FOLLOW_RECENT_WINDOW : -FOLLOW_RECENT_WINDOW]
    recent_rate = _win_rate(recent)

    # Trend: recent window vs. the one before it — a direction, not a fragile
    # count. Only named once both windows carry enough outcomes to mean something.
    trend = None
    if len(recent) >= FOLLOW_TREND_MIN and len(prior) >= FOLLOW_TREND_MIN:
        delta = (recent_rate or 0.0) - (_win_rate(prior) or 0.0)
        trend = (
            "up" if delta > FOLLOW_TREND_DELTA
            else "down" if delta < -FOLLOW_TREND_DELTA
            else "steady"
        )

    # Return-after-lapse: within the recent window, did a >= gap-day break sit
    # between two logged outcomes? If so the later one is a comeback worth marking.
    # Compare *calendar* days (not the wall-clock delta, whose ``.days`` floors a
    # 3d23h gap to 3 and would miss a genuine lapse) — matching how the rest of the
    # codebase thresholds day counts.
    recent_ts = [ts for ts, _o in rows[-FOLLOW_RECENT_WINDOW:]]
    returned = any(
        a is not None and b is not None
        and (b.date() - a.date()).days >= FOLLOW_LAPSE_GAP_DAYS
        for a, b in zip(recent_ts, recent_ts[1:], strict=False)
    )

    # Personal best: the best completion rate over any full-length run. It never
    # decreases as history grows, so it can't be lost. None until there's a run.
    best_rate = None
    if total >= FOLLOW_RECENT_WINDOW:
        best_hits = max(
            outs[i : i + FOLLOW_RECENT_WINDOW].count("success")
            for i in range(total - FOLLOW_RECENT_WINDOW + 1)
        )
        best_rate = round(best_hits / FOLLOW_RECENT_WINDOW, 2)

    return {
        "n": total,
        "counts": counts,
        "rate": rate,
        "recent_rate": recent_rate,
        "trend": trend,
        "returned": returned,
        "best_rate": best_rate,
        "series": outs[-FOLLOW_SERIES_LEN:],
    }


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


def _chores(store: MemoryStore) -> dict[str, Any]:
    """Chore completions over the last :data:`CHORE_WINDOW_DAYS`: totals, per-person, cadence.

    Reads ``household_chore_log`` — the same ledger the ntfy Done tap and the
    card's day selector write — one row per (chore, local day). Completions are
    already keyed by local ``done_on``, so a late-evening tap lands on the right
    day. Returns a per-person split, a recent day-by-day series for a sparkline,
    and today/yesterday counts. Safe/zeroed on an empty history or no household.
    """
    try:
        rows = store.chore_log_since(
            (
                local_datetime(utcnow(), get_settings().timezone).date()
                - timedelta(days=CHORE_WINDOW_DAYS - 1)
            ).isoformat()
        )
    except RuntimeError:
        # No household (unscoped/solo store) → nothing shared to tally. Narrow to
        # RuntimeError (what _household_id raises when the user is in no household)
        # so a genuine SQL error surfaces instead of silently zeroing the view.
        rows = []
    today = local_datetime(utcnow(), get_settings().timezone).date()
    by_person = Counter((r.get("done_by_name") or "Someone") for r in rows)
    by_day = Counter(str(r.get("done_on")) for r in rows)
    active_days = len(by_day)
    series = [
        {
            "day": (day := (today - timedelta(days=i)).isoformat()),
            "count": by_day.get(day, 0),
        }
        for i in range(CHORE_SERIES_LEN - 1, -1, -1)
    ]
    try:
        enabled = sum(1 for c in store.chores() if c.get("enabled"))
    except RuntimeError:
        enabled = 0  # no household (see the chore_log_since catch above)
    return {
        "window_days": CHORE_WINDOW_DAYS,
        "total": len(rows),
        "active_days": active_days,
        "avg_per_active_day": round(len(rows) / active_days, 1) if active_days else 0.0,
        "today": by_day.get(today.isoformat(), 0),
        "yesterday": by_day.get((today - timedelta(days=1)).isoformat(), 0),
        "enabled_chores": enabled,
        "by_person": [
            {"name": name, "count": count} for name, count in by_person.most_common()
        ],
        "series": series,
    }


def _feature_usage(store: MemoryStore) -> dict[str, Any]:
    """Which features you lean on, which fire-and-get-ignored, which lie dormant.

    Joins the ``feature_events`` rollup (what was offered/engaged/invoked over the
    window) against the *universe* of features that could have shown up — every
    enabled module plus the known pull surfaces — so a feature that never fired is
    visible as **dormant** rather than merely absent. Each feature is bucketed:

    - ``using`` — you engage with it, or you open it;
    - ``ignored`` — a push feature that fires often but you rarely act on
      (a candidate to retune or mute);
    - ``dormant`` — nothing offered, engaged, or invoked in the window.

    Pure and model-free like the rest of this module. Safe before any usage event
    exists (everything is dormant) and independent of ``prefrontal learn``.
    """
    from prefrontal.modules.registry import enabled_modules

    rows = {r["feature"]: r for r in store.feature_usage_rollup(USAGE_WINDOW_DAYS)}
    push = {m.key for m in enabled_modules()}
    muted = store.muted_features()
    universe = push | set(PULL_FEATURES) | set(rows) | muted

    now = utcnow()
    features: list[dict[str, Any]] = []
    for name in sorted(universe):
        row = rows.get(name, {})
        offered = int(row.get("offered") or 0)
        engaged = int(row.get("engaged") or 0)
        invoked = int(row.get("invoked") or 0)
        total = offered + engaged + invoked
        last_used = row.get("last_used")
        days_ago = None
        # SQLite CURRENT_TIMESTAMP and utcnow() are both naive UTC; parse_ts is
        # the project's canonical (None-tolerant) reader for stored timestamps.
        seen = parse_ts(last_used)
        if seen is not None:
            days_ago = max(0, (now - seen).days)
        rate = round(engaged / offered, 2) if offered else None
        kind = "push" if name in push else "pull"

        if total == 0:
            bucket = "dormant"
        elif (
            kind == "push"
            and offered >= USAGE_IGNORED_MIN_OFFERED
            and rate is not None
            and rate < USAGE_IGNORED_RATE
        ):
            bucket = "ignored"
        else:
            bucket = "using"

        features.append({
            "feature": name,
            "kind": kind,
            "offered": offered,
            "engaged": engaged,
            "invoked": invoked,
            "engagement_rate": rate,
            "last_used": last_used,
            "days_ago": days_ago,
            "bucket": bucket,
            "muted": name in muted,
        })

    # Most-active first within the page's natural reading order (using → ignored →
    # dormant is applied in the template); here sort by recent activity so the
    # busiest features head each group.
    features.sort(key=lambda f: (f["offered"] + f["engaged"] + f["invoked"]), reverse=True)
    summary = {
        b: sum(1 for f in features if f["bucket"] == b)
        for b in ("using", "ignored", "dormant")
    }
    summary["muted"] = len(muted)
    return {"window_days": USAGE_WINDOW_DAYS, "features": features, "summary": summary}


def build_stats(store: MemoryStore) -> dict[str, Any]:
    """Assemble the Insights payload from the (scoped) user's episodes.

    Args:
        store: A user-scoped :class:`MemoryStore`.

    Returns:
        ``{time_estimation, follow_through, channels, self_care, feature_usage,
        chores, counts}`` — see the module docstring. All fields are
        JSON-serializable and safe on an empty history (counts are 0, ratios/rates
        are ``None``).
    """
    episodes = store.all_episodes()
    return {
        "time_estimation": _time_estimation(episodes),
        "follow_through": _follow_through(episodes),
        "channels": _channels(episodes),
        "self_care": _self_care(store),
        "feature_usage": _feature_usage(store),
        "chores": _chores(store),
        "counts": {"episodes": len(episodes)},
    }
