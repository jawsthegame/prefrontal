"""End-of-day self-care **gap** analysis — the day read back through its timing.

The self-care module (:mod:`prefrontal.modules.self_care`) already *tracks* every
click: each Ate / Drank / Went / … confirm logs a ``self_care`` episode stamped
with the moment it happened. Adherence — "did you hit six glasses?" — is answered
by the Insights card (:mod:`prefrontal.stats`). What that count can't see is the
**shape** of the day: you can drink all six glasses and still have had your first
at 3pm, or take enough bio breaks but go six hours between two of them.

This module reads today's confirms back as a *timeline* and surfaces the gaps a
raw tally hides:

- **late first** — the first confirm landed hours after the check's start hour
  ("your first glass of water wasn't until 3:10 pm — about 6h after your 9:00
  start"). Fires even when the daily target was met, because *when* matters.
- **long gap** — the largest stretch between two consecutive confirms ran well
  past the check's cadence ("6h 10m between bio breaks — aim ~every 120m").
- **shortfall** — a quota check finished the day under its target ("3 of 6
  glasses of water — 3 short").
- **none** — an enabled check went the whole day with nothing logged.

Everything here is a **pure read** over episodes + coaching state — no writes, no
side effects — so the three surfaces share one analysis:

- ``prefrontal self-care review`` (CLI) and ``GET /self-care/review`` (JSON) — a
  pull anyone can run any time, showing gaps *and* what went well.
- an opt-in evening **push** — :class:`~prefrontal.modules.self_care.SelfCareModule`
  fires :func:`review_message` once at day's end when (and only when) there's a
  gap worth naming, so a clean day stays silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from prefrontal.clock import (
    fmt_ts,
    local_datetime,
    local_day_bounds,
    parse_ts,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.self_care import CHECKS, SELF_CARE_EPISODE, BasicCheck

#: A first confirm this many hours (or more) after the check's start hour reads as
#: a *late start* — the "you drank enough, but not until 3pm" gap. Deliberately
#: generous so a merely-slow morning isn't flagged; only a genuinely late first.
LATE_FIRST_HOURS = 3.0
#: A gap between two consecutive confirms counts as *long* once it reaches the
#: check's interval times this factor — i.e. you skipped roughly this many cycles.
LONG_GAP_FACTOR = 2.5
#: …but never flag a gap shorter than this in absolute terms, so a short-interval
#: check (a 40-min meal re-ask) doesn't cry "long gap" over an ordinary hour.
LONG_GAP_FLOOR_MINUTES = 90.0
#: How many self-care episodes to scan for a single day's confirms. A day's worth
#: is tiny; this bounds the read on a long-lived DB.
_SCAN_LIMIT = 2000

#: Per-check display copy: ``(singular, plural, mass)``. *singular* names one
#: confirm ("glass of water"), *plural* several ("glasses of water"), *mass* the
#: uncountable form for "no ___ logged today" ("water"). Falls back to the key.
_LABEL: dict[str, tuple[str, str, str]] = {
    "meal": ("meal", "meals", "meals"),
    "water": ("glass of water", "glasses of water", "water"),
    "meds": ("meds dose", "meds doses", "meds"),
    "biobreak": ("bio break", "bio breaks", "bio breaks"),
    "winddown": ("wind-down", "wind-downs", "wind-down"),
    "movement": ("movement break", "movement breaks", "movement"),
}


#: Per-check display title for a review row / gap line (falls back to ``Key``).
_TITLE: dict[str, str] = {
    "meal": "Meals",
    "water": "Water",
    "meds": "Meds",
    "biobreak": "Bio breaks",
    "winddown": "Wind-down",
    "movement": "Movement",
}


def _labels(key: str) -> tuple[str, str, str]:
    return _LABEL.get(key, (key, f"{key}s", key))


def _title(key: str) -> str:
    return _TITLE.get(key, key.title())


def _fmt_clock(dt: datetime) -> str:
    """A local datetime as a friendly 12-hour clock, e.g. ``"3:10 pm"``."""
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{hour12}:{dt.minute:02d} {ampm}"


def _fmt_dur(minutes: float) -> str:
    """Whole-minute duration as ``"6h 10m"`` / ``"45m"`` / ``"2h"``."""
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


@dataclass(frozen=True)
class _CheckConfig:
    """The gate + cadence knobs one check contributes to the review (read-only)."""

    key: str
    enabled: bool
    open_ended: bool
    target: int
    start_hour: int
    interval: int


def _check_config(store: MemoryStore, check: BasicCheck, master_on: bool) -> _CheckConfig:
    """Resolve a check's live config from coaching state (mirrors the module).

    Reads only public store accessors + the public :data:`CHECKS` metadata, so
    this stays a leaf that never reaches into the module's private helpers.
    """
    enabled = master_on and (store.get_state(check.enabled_key, "on") or "on") == "on"
    return _CheckConfig(
        key=check.key,
        enabled=enabled,
        open_ended=check.open_ended,
        target=max(1, int(store.get_float(check.target_key, check.target_default))),
        start_hour=store.get_hour(check.start_hour_key, check.start_hour_default),
        interval=max(1, int(store.get_float(check.interval_key, check.interval_default))),
    )


def _confirms_today(
    store: MemoryStore, now: datetime, tz: str
) -> dict[str, list[datetime]]:
    """Local confirm times, per check key, for the local day containing ``now``.

    Each ``self_care`` confirm episode carries ``context = "<key>: confirmed"`` and
    a UTC timestamp; this buckets the *confirmed* ones by key and projects them to
    the user's wall clock (so a late-evening tap counts toward the right local
    day). Snoozes and ignores are excluded — a gap is about what you *did*. Times
    come back sorted ascending.
    """
    day_start, day_end = local_day_bounds(now, tz)
    known = {c.key for c in CHECKS}
    by_key: dict[str, list[datetime]] = {}
    for e in store.episodes_by_type(SELF_CARE_EPISODE, limit=_SCAN_LIMIT):
        if e.get("outcome") != "confirmed":
            continue
        context = str(e.get("context") or "")
        key, _, verb = context.partition(": ")
        if verb != "confirmed" or key not in known:
            continue
        ts = parse_ts(e.get("timestamp"))
        if ts is None or ts < day_start or ts >= day_end:
            continue
        by_key.setdefault(key, []).append(local_datetime(ts, tz))
    for times in by_key.values():
        times.sort()
    return by_key


def _review_check(
    cfg: _CheckConfig, confirms: list[datetime], local_now: datetime
) -> dict[str, Any]:
    """Analyse one check's confirm timeline into findings + a rendered line.

    ``findings`` holds only the *gaps* (``none`` / ``shortfall`` / ``late_first`` /
    ``long_gap``); ``on_track`` is set when the check met its bar with none of
    them. Pure — the caller owns all I/O.
    """
    singular, plural, mass = _labels(cfg.key)
    count = len(confirms)
    findings: list[dict[str, str]] = []
    started = local_now.hour >= cfg.start_hour

    # 1) Nothing at all today (only a gap once the check's window has opened).
    if count == 0:
        if started:
            findings.append({"kind": "none", "text": f"no {mass} logged today"})
        return _check_row(cfg, count, confirms, findings, on_track=False)

    # 2) Late first confirm — even a met target can start too late in the day.
    first = confirms[0]
    start_dt = first.replace(hour=cfg.start_hour, minute=0, second=0, microsecond=0)
    hours_late = (first - start_dt).total_seconds() / 3600.0
    if hours_late >= LATE_FIRST_HOURS:
        findings.append(
            {
                "kind": "late_first",
                "text": (
                    f"first {singular} wasn't until {_fmt_clock(first)} — "
                    f"about {_fmt_dur(hours_late * 60)} after your {cfg.start_hour}:00 start"
                ),
            }
        )

    # 3) Long gap — the widest stretch between two consecutive confirms.
    if count >= 2:
        gaps = [
            (b - a).total_seconds() / 60.0
            for a, b in zip(confirms, confirms[1:], strict=False)
        ]
        widest = max(gaps)
        threshold = max(LONG_GAP_FLOOR_MINUTES, cfg.interval * LONG_GAP_FACTOR)
        if widest >= threshold:
            findings.append(
                {
                    "kind": "long_gap",
                    "text": (
                        f"{_fmt_dur(widest)} between {plural} — aim ~every "
                        f"{cfg.interval}m"
                    ),
                }
            )

    # 4) Shortfall — a quota check that finished the day under target.
    if not cfg.open_ended and cfg.target > 1 and count < cfg.target:
        short = cfg.target - count
        findings.append(
            {
                "kind": "shortfall",
                "text": f"{count} of {cfg.target} {plural} — {short} short",
            }
        )

    met = cfg.open_ended or count >= cfg.target
    on_track = met and not findings
    return _check_row(cfg, count, confirms, findings, on_track=on_track)


def _check_row(
    cfg: _CheckConfig,
    count: int,
    confirms: list[datetime],
    findings: list[dict[str, str]],
    *,
    on_track: bool,
) -> dict[str, Any]:
    """Assemble one check's review row (its findings, a win token, a summary line)."""
    win = _win_token(cfg, count) if on_track else None
    if findings:
        line = "; ".join(f["text"] for f in findings)
    elif win:
        line = f"on track ({win})"
    else:
        line = "nothing logged today"
    return {
        "key": cfg.key,
        "title": _title(cfg.key),
        "open_ended": cfg.open_ended,
        "count": count,
        "target": cfg.target,
        "first": _fmt_clock(confirms[0]) if confirms else None,
        "findings": findings,
        "on_track": on_track,
        "win": win,
        "line": line,
    }


def _win_token(cfg: _CheckConfig, count: int) -> str:
    """A short "what went well" token for a check that met its bar cleanly."""
    if cfg.open_ended:
        return f"{cfg.key} ×{count}"
    if cfg.target > 1:
        return f"{cfg.key} {count}/{cfg.target}"
    return cfg.key


def self_care_review(store: MemoryStore, now: datetime, tz: str) -> dict[str, Any]:
    """Today's end-of-day self-care gap analysis for the surfaces (pure read).

    Fans over every **enabled** check, reads its confirm timeline for the local
    day, and derives per-check findings (see the module docstring). Returns the
    per-check rows plus flattened ``gaps`` / ``wins`` lists and the ``has_findings``
    flag the evening push gates on. No writes.

    Args:
        store: A user-scoped store.
        now: The current instant as naive UTC.
        tz: IANA timezone for "today" and the wall-clock projection.

    Returns:
        ``{date, generated_at, enabled, checks, gaps, wins, has_findings,
        has_content}``. ``enabled`` is the master switch; when it's off, ``checks``
        is empty. ``gaps`` are ``"<Title> — <finding>"`` lines; ``wins`` are short
        tokens. ``has_content`` is true when anything at all was logged or is due.
    """
    local = local_datetime(now, tz)
    today = local.strftime("%Y-%m-%d")
    master_on = (store.get_state("self_care", "off") or "off") == "on"
    base: dict[str, Any] = {
        "date": today,
        "generated_at": fmt_ts(now),
        "enabled": master_on,
        "checks": [],
        "gaps": [],
        "wins": [],
        "has_findings": False,
        "has_content": False,
    }
    if not master_on:
        return base

    confirms = _confirms_today(store, now, tz)
    checks: list[dict[str, Any]] = []
    gaps: list[str] = []
    wins: list[str] = []
    for check in CHECKS:
        cfg = _check_config(store, check, master_on)
        if not cfg.enabled:
            continue
        row = _review_check(cfg, confirms.get(check.key, []), local)
        checks.append(row)
        for f in row["findings"]:
            gaps.append(f"{row['title']} — {f['text']}")
        if row["win"]:
            wins.append(row["win"])

    base["checks"] = checks
    base["gaps"] = gaps
    base["wins"] = wins
    base["has_findings"] = bool(gaps)
    base["has_content"] = any(c["count"] for c in checks) or bool(gaps)
    return base


def render_review(review: dict[str, Any]) -> str:
    """Render a review for the CLI / a text surface (multi-line, human-readable)."""
    if not review["enabled"]:
        return "Self-care checks are off — turn them on to track this."
    date = review["date"]
    if not review["checks"]:
        return f"Self-care — end of day ({date}): no checks are enabled."
    lines = [f"Self-care — end of day ({date})", ""]
    if review["gaps"]:
        lines.append("Gaps:")
        lines.extend(f"  • {g}" for g in review["gaps"])
    else:
        lines.append("No gaps today — nicely spaced. 🎉")
    if review["wins"]:
        lines.append("")
        lines.append(f"On track: {', '.join(review['wins'])}")
    return "\n".join(lines)


def review_message(review: dict[str, Any], name: str = "") -> str | None:
    """The evening push body, or ``None`` when there's no gap worth interrupting for.

    The proactive counterpart to :func:`render_review`: it leads with the gaps
    (the whole reason to buzz at day's end) and appends a one-line "what went
    well" so it never reads as pure scolding. Returns ``None`` — so the caller
    simply doesn't fire — when self-care is off or the day had no gaps, keeping a
    clean day silent.
    """
    if not review["enabled"] or not review["has_findings"]:
        return None
    lead = f"{name}, end-of-day check-in" if name else "End-of-day self-care check-in"
    body = [f"{lead} — a couple of gaps to notice:"]
    body.extend(f"• {g}" for g in review["gaps"])
    if review["wins"]:
        body.append("")
        body.append(f"Went well: {', '.join(review['wins'])}. 👏")
    return "\n".join(body)
