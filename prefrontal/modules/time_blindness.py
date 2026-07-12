"""Time blindness module.

Time blindness is difficulty perceiving how much time has passed or how long
something will take. Its signature in the memory layer is the ``time_estimation``
pattern and the ``time_estimation_bias`` coaching value: tasks and departures
consistently take longer than predicted.

This module turns that data into concrete guidance (apply the learned multiplier,
add departure buffers) and declares the interventions that act on it. It also owns
the two bookends of an obligation you have to *be somewhere* for: the morning-of
``departure_buffer`` ("leave now") and the night-before ``morning_prep`` heads-up
("early start tomorrow — set an alarm"), so an early commitment doesn't fall out
of mind until you're rushing.
"""

from __future__ import annotations

import re
from datetime import datetime

from prefrontal.coaching import CoachContext, Cue
from prefrontal.departure import (
    build_departure_message,
    next_departure,
    plan_upcoming_departures,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Default interval (minutes) between elapsed-time callouts during a focus block.
#: ``0`` disables them — off unless the user opts in, since not everyone wants a
#: periodic ping (and an aligned deep block is otherwise protected by Hyperfocus).
DEFAULT_ELAPSED_CALLOUT_MINUTES = 0

#: Departure escalation level → coaching urgency, so the departure_buffer
#: intervention rides the coaching tick (and its native `coach --deliver`
#: delivery) instead of a separate n8n poll of /webhooks/departure/check. `go`
#: maps to ``critical`` so the "head out now" nudge bypasses quiet hours (a hard
#: leave-by is time-critical), mirroring how the endpoint fires regardless.
DEPARTURE_URGENCY = {"heads_up": "nudge", "soon": "urgent", "go": "critical"}

#: Local clock time before which tomorrow's earliest leave-by counts as an "early
#: start" worth an evening heads-up. Compared against the *leave-by* (not the raw
#: start), so a 9am appointment 45 min away still qualifies. ``HH:MM``, tunable
#: via the ``early_start_threshold`` coaching-state key.
DEFAULT_EARLY_START_THRESHOLD = "08:30"
DEFAULT_EARLY_START_HM = (8, 30)

#: Local hour to send the evening "set an alarm" heads-up. The module only emits
#: the cue at or after this hour; the coaching engine's quiet hours (responsive
#: window, default …–22:00) cap the top, so the effective window is
#: ``[morning_prep_hour, responsive_end)``. When the configured hour is at or past
#: ``responsive_end`` (a user who winds down early), that window would be empty and
#: the nudge would never deliver, so it's clamped into the last responsive hour.
#: Tunable via ``morning_prep_hour``.
DEFAULT_MORNING_PREP_HOUR = 21

#: Minutes of morning routine (shower, breakfast, …) subtracted from the leave-by
#: to suggest a wake time for the one-tap "Set alarm" button. Tunable via
#: ``morning_routine_minutes`` — and *learned* from real early-start departures by
#: :func:`adapt_morning_routine`.
DEFAULT_MORNING_ROUTINE_MINUTES = 60

#: Bounds the learned morning-routine lead so a run of bad mornings (or a spell of
#: leaving very early) can't drive the suggested wake time to something absurd.
MIN_ROUTINE_MINUTES = 20
MAX_ROUTINE_MINUTES = 180
#: Fewest early-start departures we need before adapting the routine lead at all.
MIN_ROUTINE_SAMPLES = 4
#: A mean lateness inside ±this many minutes reads as "about right" — don't churn
#: the lead over a rounding-scale wobble (mirrors the departure grace).
ROUTINE_DEADBAND_MINUTES = 3.0
#: How many recent ``departure`` episodes to consider when learning the lead.
ROUTINE_LOOKBACK = 40

#: Default iOS Shortcut name the "Set alarm" button runs. Mirrors
#: :data:`prefrontal.webhooks.notify.DEFAULT_ALARM_SHORTCUT` (kept as a plain
#: literal here rather than imported — the ``webhooks`` package pulls in the whole
#: FastAPI app, which would cycle during module registration). Tunable per user
#: via ``alarm_shortcut_name``.
DEFAULT_ALARM_SHORTCUT = "Set Alarm"


def elapsed_callout_message(task: str, minutes: int, name: str = "") -> str:
    """A gentle 'you've been on this N min' time check (no judgment, no ask)."""
    greeting = f"{name}, a" if name else "A"
    task_clause = f" on '{task}'" if task else ""
    return (
        f"⏳ {greeting} time check: about {minutes} min{task_clause} so far. "
        "Just so it doesn't slip by — carry on if you're in a good groove."
    )


def parse_clock_hm(raw: object, default: tuple[int, int]) -> tuple[int, int]:
    """Parse an ``HH:MM`` coaching-state clock value to ``(hour, minute)``.

    The early-start threshold is genuinely half-past-friendly ("08:30"), so unlike
    :func:`prefrontal.clock.parse_hour` (which drops the minutes) this keeps them.
    Tolerant of ``None`` / garbage / a bare hour ("8") — anything unparseable or
    out of range falls back to ``default`` rather than raising.
    """
    if raw is None:
        return default
    parts = str(raw).strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
    except (TypeError, ValueError):
        return default
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return default


def early_morning_plans(
    plans: list, now: datetime, tz: str, threshold_hour: int, threshold_minute: int
) -> list:
    """Departure plans for *tomorrow's* early starts, soonest leave-by first.

    A plan qualifies when its commitment starts on tomorrow's local date **and**
    its leave-by falls at or before ``threshold_hour:threshold_minute`` local time
    that morning. Keying off the *leave-by* rather than the raw start is the point:
    a 9am appointment 45 min away (leave-by ~8:00) is an early start worth an alarm
    even though it "starts" after the cutoff, while a leisurely late-morning thing
    is not. Full instants are compared (not bare clock times), so the rare
    pre-midnight leave-by still attaches to the morning it's for.

    Args:
        plans: :class:`~prefrontal.departure.DeparturePlan`\\s (one per upcoming
            commitment), as returned by ``plan_upcoming_departures``.
        now: Current instant, naive UTC.
        tz: The user's IANA timezone (the "tomorrow" and threshold are local).
        threshold_hour: Local hour of the early-start cutoff.
        threshold_minute: Local minute of the early-start cutoff.

    Returns:
        The qualifying plans, ordered by leave-by ascending (earliest first).
    """
    from datetime import timedelta

    from prefrontal.clock import local_datetime, local_time_utc, parse_ts_strict

    tomorrow = (local_datetime(now, tz) + timedelta(days=1)).date()
    cutoff = local_time_utc(now + timedelta(days=1), tz, threshold_hour, threshold_minute)
    qualifying = [
        p
        for p in plans
        if local_datetime(parse_ts_strict(p.commitment["start_at"]), tz).date() == tomorrow
        and parse_ts_strict(p.leave_by) <= cutoff
    ]
    qualifying.sort(key=lambda p: p.leave_by)
    return qualifying


def morning_prep_message(
    title: str,
    start_hhmm: str,
    *,
    where: str = "",
    leave_hhmm: str | None = None,
    extra_count: int = 0,
    name: str = "",
) -> str:
    """Phrase the evening "early start tomorrow — set an alarm" heads-up.

    ``leave_hhmm`` is included only when it differs from the start time (i.e. there
    is real travel/prep lead); attend-mode commitments pass ``None`` since you're
    already where you'll be. ``extra_count`` notes further early commitments beyond
    the first without listing them all.
    """
    greeting = f"{name}, an" if name else "An"
    out = (
        f" — plan to be out the door by ~{leave_hhmm}"
        if leave_hhmm and leave_hhmm != start_hhmm
        else ""
    )
    more = f" (plus {extra_count} more early)" if extra_count > 0 else ""
    return (
        f"🌅 {greeting} early start tomorrow: {title}{where} at {start_hhmm}{out}{more}. "
        "Worth setting an alarm tonight."
    )


# --- Learned morning-routine lead (learning loop) ----------------------------
#
# ``morning_prep``'s one-tap "Set alarm" backs a *static* ``morning_routine_minutes``
# (default 60) off the leave-by to suggest a wake time. But departure-outcome
# capture (``/webhooks/departure/left`` → :func:`prefrontal.departure.classify_departure`)
# already knows whether you actually left on time. So on early-start mornings —
# exactly the ones ``morning_prep`` fires for — feed that on-time/late signal back
# into a *learned* per-person lead: if you're chronically leaving late off a
# 60-minute lead your real get-ready span is longer (wake earlier); if you're
# reliably out with time to spare it can ease back so you sleep a little more. Same
# "it gets better the longer you use it" loop the time bias rides, run in the
# nightly ``learn`` pass and never overriding a lead the user set by hand.

#: Parses the "leave-by HH:MM" clock :func:`record_departure_outcome` writes into a
#: departure episode's notes, so the learner can tell an *early-start* departure
#: from a midday one without re-deriving the plan.
_LEAVE_BY_RE = re.compile(r"leave-by (\d{1,2}):(\d{2})")
#: The three lateness phrasings that same note uses, mapped to a signed minutes
#: reading: "~N min late" → +N, "N min to spare" → −N, "right on time" → 0.
_LATE_RE = re.compile(r"~(\d+) min late")
_SPARE_RE = re.compile(r"(\d+) min to spare")


def _departure_leave_by_minutes(notes: str | None) -> int | None:
    """Local minute-of-day of the leave-by recorded in a departure note, or ``None``."""
    m = _LEAVE_BY_RE.search(notes or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _departure_lateness(notes: str | None, outcome: str | None) -> float | None:
    """Signed lateness (minutes) read from a departure note: + late, − early, 0 on time.

    Reads the human phrasing :func:`record_departure_outcome` wrote rather than a
    stored number (the episode keeps ``actual_value=None`` by design), so it's the
    one honest signal available. Returns ``None`` when nothing parses.
    """
    if notes is None:
        return None
    late = _LATE_RE.search(notes)
    if late:
        return float(late.group(1))
    spare = _SPARE_RE.search(notes)
    if spare:
        return -float(spare.group(1))
    if "right on time" in notes:
        return 0.0
    # No phrase parsed, but the outcome still tells us the direction coarsely.
    if outcome == "success":
        return 0.0
    return None


def _bounded_routine(value: float) -> int:
    """Clamp a proposed routine lead to ``[MIN, MAX]`` minutes, rounded to 5."""
    clamped = min(max(value, MIN_ROUTINE_MINUTES), MAX_ROUTINE_MINUTES)
    return int(round(clamped / 5.0) * 5)


def adapt_morning_routine(store: MemoryStore, now: datetime | None = None) -> dict:
    """Learn ``morning_routine_minutes`` from recent early-start departures.

    Runs in the nightly ``learn`` pass. Looks at ``departure`` episodes whose
    recorded leave-by falls at or before the ``early_start_threshold`` (the
    mornings ``morning_prep`` cares about), averages how late/early you actually
    left, and nudges the suggested wake lead by that mean — later leads make you
    wake earlier, so a mean of "+12 min late" grows the lead ~12 min. Two-sided but
    bounded (:data:`MIN_ROUTINE_MINUTES`/:data:`MAX_ROUTINE_MINUTES`), inside a
    small deadband so it doesn't churn, and it never overrides a lead the user set
    explicitly. Always returns a summary dict for the CLI — ``changed`` is ``False``
    (with a ``reason``) when nothing moved, e.g. no early-start data yet or a
    user-set lead.
    """
    default = DEFAULT_MORNING_ROUTINE_MINUTES
    current = int(store.get_float("morning_routine_minutes", default))
    user_set = (
        (store.all_state().get("morning_routine_minutes", {}) or {}).get("source") == "explicit"
    )
    hour, minute = parse_clock_hm(
        store.get_state("early_start_threshold"), DEFAULT_EARLY_START_HM
    )
    threshold_min = hour * 60 + minute

    latenesses: list[float] = []
    for ep in store.episodes_by_type("departure", limit=ROUTINE_LOOKBACK):
        notes = ep.get("notes")
        leave_by = _departure_leave_by_minutes(notes)
        if leave_by is None or leave_by > threshold_min:
            continue  # not an early-start morning — routine lead doesn't apply
        signed = _departure_lateness(notes, ep.get("outcome"))
        if signed is not None:
            latenesses.append(signed)

    if len(latenesses) < MIN_ROUTINE_SAMPLES:
        return {
            "routine": current,
            "changed": False,
            "samples": len(latenesses),
            "reason": "not enough early-start departures yet",
        }
    mean_late = sum(latenesses) / len(latenesses)
    if user_set:
        return {
            "routine": current,
            "changed": False,
            "samples": len(latenesses),
            "reason": "held (you set this lead)",
        }
    if abs(mean_late) <= ROUTINE_DEADBAND_MINUTES:
        return {
            "routine": current,
            "changed": False,
            "samples": len(latenesses),
            "reason": "early-start departures land on time — lead looks right",
        }
    suggested = _bounded_routine(current + mean_late)
    if suggested == current:
        return {
            "routine": current,
            "changed": False,
            "samples": len(latenesses),
            "reason": "already at the bound for that trend",
        }
    store.set_state("morning_routine_minutes", str(suggested), source="inferred")
    if mean_late > 0:
        reason = f"often leaving ~{round(mean_late)} min late on early starts — waking you earlier"
    else:
        reason = f"leaving with ~{round(-mean_late)} min to spare — easing the wake time back"
    return {"routine": suggested, "changed": True, "samples": len(latenesses), "reason": reason}


class TimeBlindnessModule(Module):
    """Corrects for underestimated durations and missed departure times."""

    key = "time_blindness"
    title = "Time Blindness"
    challenge = (
        "Difficulty sensing elapsed time and estimating task/travel duration, "
        "leading to chronic underestimation and late departures."
    )
    default_state = {
        "time_estimation_bias": "1.4",
        "departure_buffer_minutes": "10",
        # Off by default; set to e.g. 30 to get a gentle time check every 30 min
        # during a focus block (elapsed_time_callouts).
        "elapsed_callout_minutes": str(DEFAULT_ELAPSED_CALLOUT_MINUTES),
        # Evening "set an alarm" heads-up for an early start tomorrow: fire at/after
        # this local hour when tomorrow's earliest leave-by is before the threshold.
        "early_start_threshold": DEFAULT_EARLY_START_THRESHOLD,
        "morning_prep_hour": str(DEFAULT_MORNING_PREP_HOUR),
        # The nudge's one-tap "Set alarm" button deep-links to this iOS Shortcut,
        # passing a wake time = leave-by minus this many minutes of morning routine.
        "alarm_shortcut_name": DEFAULT_ALARM_SHORTCUT,
        "morning_routine_minutes": str(DEFAULT_MORNING_ROUTINE_MINUTES),
    }

    def channel_targets(self) -> dict[str, str]:
        """Departure nudges carry one-tap buttons, keyed by ``commitment_id``."""
        return {"departure": "commitment_id"}

    def interventions(self) -> list[Intervention]:
        """Declare time-awareness interventions."""
        return [
            Intervention(
                name="estimate_correction",
                description="Multiply the agent's raw time estimates by the learned bias.",
                trigger="any prediction of task or travel duration",
                status="active",
            ),
            Intervention(
                name="departure_buffer",
                description="Add a learned buffer and nudge earlier than the naive leave time.",
                trigger="an upcoming calendar event with a location",
                status="active",
            ),
            Intervention(
                name="elapsed_time_callouts",
                description="Periodic 'you've been on this N minutes' callouts during a block.",
                trigger="an active focus/work block",
                status="active",
            ),
            Intervention(
                name="morning_prep",
                description="The night before an early start, nudge to set an alarm.",
                trigger="a commitment tomorrow whose leave-by is before the early-start threshold",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit the departure reminder (always) and elapsed-time callouts (opt-in).

        First, the **departure_buffer** cue: the most-urgent upcoming commitment
        whose leave-by has come due, so `coach --deliver` sends "leave by" nudges
        without n8n polling ``/webhooks/departure/check`` (fire-once + escalation
        are the engine's job — see :data:`DEPARTURE_URGENCY`).

        Then the **elapsed-time callouts**, silent unless ``elapsed_callout_minutes``
        > 0. For each active
        focus session, once it's run at least one interval, fires a low-key "N min
        so far" cue on each interval boundary — deduped per elapsed *bucket* so a
        tick running every few minutes fires each mark exactly once. This is the
        time-awareness counterpart to Hyperfocus's interrupts: Hyperfocus owns the
        alignment *check* and biological *break* (on ``/webhooks/focus/check``);
        this only surfaces elapsed time for someone who's opted in to piercing
        their own time blindness. ``nudge`` urgency — a gentle push, not a digest.
        """
        cues: list[Cue] = []

        # Departure reminder (the departure_buffer intervention) as a coach cue, so
        # a single `coach --deliver` tick delivers "leave by" nudges natively and
        # the n8n departure workflow can retire. Reuses the same pure planners the
        # /webhooks/departure/check endpoint and the widget's leave-by both use, so
        # the nudge matches the displayed leave-by. Fire-once is the engine's
        # per-dedup_key debounce (the key carries the level, so each
        # heads_up→soon→go transition is a fresh fire); `go` is critical, so the
        # final "head out now" bypasses quiet hours like the endpoint does.
        plans = plan_upcoming_departures(
            store, current_lat=ctx.current_lat, current_lon=ctx.current_lon
        )
        top = next_departure(plans)
        if top is not None:
            message = build_departure_message(top, name=ctx.display_name)
            # Attend-mode heads_up/soon return "" (nothing to say until it starts) —
            # only emit a cue when there's real text.
            if message:
                cid = top.commitment["id"]
                cues.append(
                    Cue(
                        module=self.key,
                        intervention="departure_buffer",
                        urgency=DEPARTURE_URGENCY.get(top.level, "nudge"),
                        text=message,
                        context_key="departure",
                        dedup_key=f"departure:{cid}:{top.level}",
                        ref={"commitment_id": cid},
                    )
                )

        # Evening "early start tomorrow — set an alarm" heads-up (morning_prep).
        # Reuses the same plans, so its leave-by matches the morning nudge's.
        cues.extend(self._morning_prep_cues(store, plans, ctx))

        interval = int(store.get_float("elapsed_callout_minutes", DEFAULT_ELAPSED_CALLOUT_MINUTES))
        if interval <= 0:
            return cues
        for session in store.active_focus_sessions():
            elapsed = session.get("elapsed_minutes") or 0.0
            if elapsed < interval:
                continue  # not past the first mark yet
            bucket = int(elapsed // interval)  # 1 at the first interval, 2 at the second…
            minutes = bucket * interval  # the mark we're calling out (stable per bucket)
            cues.append(
                Cue(
                    module=self.key,
                    intervention="elapsed_time_callouts",
                    urgency="nudge",
                    text=elapsed_callout_message(
                        session.get("intended_task") or "", minutes, ctx.display_name
                    ),
                    context_key="focus",
                    dedup_key=f"elapsed_callout:{session['id']}:{bucket}",
                    ref={"session_id": session["id"], "elapsed_minutes": minutes},
                    suggested_channel="push",
                )
            )
        return cues

    def _morning_prep_cues(self, store: MemoryStore, plans: list, ctx: CoachContext) -> list[Cue]:
        """The evening "early start tomorrow — set an alarm" heads-up (morning_prep).

        Only fires at or after ``morning_prep_hour`` local (the engine's quiet hours
        cap the top of the window). When tomorrow's earliest leave-by is before the
        ``early_start_threshold``, emits one ``nudge`` cue for that commitment —
        ``dedup_key`` carries tomorrow's date + the commitment id, so it fires once
        the evening before and won't repeat on later ticks that same evening. This
        is the night-before bookend of ``departure_buffer``: that one says "leave
        now" in the morning; this one says "you'll need to be up early, set an
        alarm" the night before, so an early obligation doesn't get forgotten until
        you're rushing.
        """
        from datetime import timedelta

        from prefrontal.clock import local_datetime, local_hour_of, parse_hour, parse_ts_strict

        prep_hour = parse_hour(store.get_state("morning_prep_hour"), DEFAULT_MORNING_PREP_HOUR)
        # This heads-up is a non-critical ``nudge``, so the coaching engine's quiet
        # hours hold it whenever it's generated outside the responsive window (see
        # coaching.suppressed). That makes the true send window
        # ``[prep_hour, responsive_end)``: if the configured prep hour lands at or
        # after the hour quiet time begins, that window is empty — the "set an alarm"
        # cue is regenerated every evening but suppressed on every tick, so the alarm
        # nudge silently never arrives. Someone who winds down early (responsive
        # hours ending at, say, 21:00) would never get it. Clamp the start into the
        # last responsive hour so there is always a live window. Only for a normal,
        # non-wrapping daytime band; a wrapped/degenerate window (start >= end)
        # imposes no evening ceiling, so leave the configured hour alone.
        if ctx.responsive_start < ctx.responsive_end and prep_hour >= ctx.responsive_end:
            prep_hour = ctx.responsive_end - 1
        if local_hour_of(ctx.now, ctx.timezone) < prep_hour:
            return []  # not yet the evening send window
        hour, minute = parse_clock_hm(
            store.get_state("early_start_threshold"), DEFAULT_EARLY_START_HM
        )
        early = early_morning_plans(plans, ctx.now, ctx.timezone, hour, minute)
        if not early:
            return []
        p = early[0]
        cid = p.commitment["id"]
        start_local = local_datetime(parse_ts_strict(p.commitment["start_at"]), ctx.timezone)
        leave_local = local_datetime(parse_ts_strict(p.leave_by), ctx.timezone)
        location = p.commitment.get("location")
        # A suggested wake time for the one-tap "Set alarm" button: back off the
        # morning routine from when you must be up-and-moving (leave-by for a travel
        # commitment; the start itself when you attend from here).
        routine = int(store.get_float("morning_routine_minutes", DEFAULT_MORNING_ROUTINE_MINUTES))
        up_by = start_local if p.mode == "attend" else leave_local
        # tz-ok: up_by is already local (start_local/leave_local, both via local_datetime)
        wake_at = (up_by - timedelta(minutes=max(0, routine))).strftime("%H:%M")
        shortcut = store.get_state("alarm_shortcut_name") or DEFAULT_ALARM_SHORTCUT
        return [
            Cue(
                module=self.key,
                intervention="morning_prep",
                urgency="nudge",
                text=morning_prep_message(
                    p.commitment.get("title") or "an early commitment",
                    start_local.strftime("%H:%M"),
                    where=f" ({location})" if location else "",
                    # Attend mode has no meaningful "be out by" — you're already there.
                    leave_hhmm=None if p.mode == "attend" else leave_local.strftime("%H:%M"),
                    extra_count=len(early) - 1,
                    name=ctx.display_name,
                ),
                context_key="morning_prep",
                dedup_key=f"morning_prep:{start_local.date().isoformat()}:{cid}",
                ref={
                    "commitment_id": cid,
                    "start_at": p.commitment["start_at"],
                    "leave_by": p.leave_by,
                    # Payload for the one-tap "Set alarm" button (see
                    # notify.alarm_actions_for_cue) — a suggested wake time and the
                    # iOS Shortcut to run.
                    "alarm_at": wake_at,
                    "alarm_shortcut": shortcut,
                },
                suggested_channel="push",
            )
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize the learned time bias and any time-estimation patterns."""
        lines: list[str] = []
        bias = store.get_state("time_estimation_bias")
        if bias:
            try:
                pct = round((float(bias) - 1.0) * 100)
                lines.append(
                    f"Apply a **{bias}x** multiplier to all time estimates "
                    f"(historical ~{pct}% underestimate)."
                )
            except ValueError:
                pass
        buffer = store.get_state("departure_buffer_minutes")
        if buffer:
            lines.append(f"Add a **{buffer}-minute** buffer before departure nudges.")
        callout = int(store.get_float("elapsed_callout_minutes", 0))
        if callout > 0:
            lines.append(f"Send a gentle time check every **{callout} min** during a focus block.")
        threshold = store.get_state("early_start_threshold")
        if threshold:
            routine = int(
                store.get_float("morning_routine_minutes", DEFAULT_MORNING_ROUTINE_MINUTES)
            )
            lines.append(
                f"The evening before, nudge to set an alarm when tomorrow's earliest "
                f"leave-by is before **{threshold}** (suggest waking ~**{routine} min** "
                f"before you must be up — learned from how early-start mornings go)."
            )

        patterns = store.get_patterns("time_estimation")
        for p in patterns:
            variance = p.get("variance")
            if variance:
                lines.append(
                    f"`{p['context_key']}` runs {abs(variance)} over estimate "
                    f"(confidence {(p.get('confidence') or 0):.0%})."
                )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(TimeBlindnessModule())
