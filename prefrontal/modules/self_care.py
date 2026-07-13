"""Self-care module — the basic-needs checks that a focus state quietly drops.

Most Prefrontal modules answer *how your ADHD shows up* (time blindness,
hyperfocus, …). This one covers a blunter failure mode: in a deep enough focus
state you forget to eat or drink. So these are the cues that deliberately
**override the protect-the-flow stance** — a flow state is exactly when the
reminder is needed, not suppressed.

It's a small **registry of basic-needs checks**, each riding the coaching-agent
tick (:meth:`Module.evaluate`) so responsive-hours + debounce come for free (no
overnight nag). They differ mainly in their **daily target** (and, for a
window-bounded one, an **end hour**):

- **meal** (target 1) — from ``meal_start_hour`` (default 11), ask "have you
  eaten?" and re-ask every ``meal_reask_minutes`` until you confirm; one "Ate"
  meets the target and it goes quiet for the day.
- **water** (target ``water_daily_target``, default 6) — from ``water_start_hour``
  (default 9), a "drink some water" reminder every ``water_interval_minutes``
  (default 90); each **Drank** counts one toward the target and pushes the next
  reminder out a full interval, and once you hit the target it's done for the day.
- **meds** (target 1) — **off by default** even when self-care is on, because
  medication is personal (opt in via ``meds_enabled``).
- **biobreak** (**open-ended**) — on with self-care, like meal/water, but *not a
  quota*: it's a plain interval reminder, not a count-to-N goal. A "take a
  bathroom break" nudge fires every ``biobreak_interval_minutes`` (default 120)
  from 8:00 within a **window** — bounded by an **end hour** (``biobreak_end_hour``,
  default 19), never by a daily total. No count silences it; tapping **Went** just
  defers the next reminder a full interval (and logs how you responded), and it's
  never "done for the day". The point is the nudge + your response, not hitting a
  number.
- **winddown** (target 1) — **off by default** even when self-care is on, because
  a bedtime is a personal preference (like meds). From ``winddown_start_hour``
  (default 21, in the *evening*) it nudges "start winding down for bed" every
  ``winddown_reask_minutes`` until one "Winding down" settles it for the night.
  Unlike the daytime checks it lives right up against the responsive-hours edge:
  it deliberately leans on the engine's quiet-hours gate (a cue outside responsive
  hours is suppressed) so it never nags past bedtime, rather than modelling a
  bedtime itself.
- **movement** (target 1) — **off by default** even when self-care is on, because
  an exercise cadence is personal (like meds/winddown). The daily-movement
  *floor*: a tiny, always-doable minimum (a few minutes of stretching) that
  survives the worst day, so a movement habit never hits zero. From
  ``movement_start_hour`` (default 7, the *morning* — anchored to the first
  coffee, an existing daily pause you habit-stack the stretch onto) it nudges
  "time to move" every ``movement_reask_minutes`` until one "Stretched" settles it
  for the day. The re-ask until met is the point: blowing past the exact minute
  doesn't lose the day, which is what makes it survivable for time-blindness. (A
  7:00 start sits below the default 8:00 responsive-hours window, so it's first
  *delivered* at 8 unless ``responsive_hours_start`` is widened to 7 — see the
  ``DEFAULT_MOVEMENT_START_HOUR`` note.)

So "how many yeses stops it for the day" is just the target (1 for a meal, N for
water) — except for an open-ended check, which no count ever stops; a check with
an end hour also stops at it, target met or not. Off by
default — set the ``self_care`` coaching key to ``on`` (each check can then be
turned off individually with ``meal_enabled`` / ``water_enabled`` / …).

Cadence without fighting the engine debounce: a cue's ``dedup_key`` carries a
per-interval *bucket* (which window of the day we're in), so each window fires
exactly once — the engine's fire-once-per-``dedup_key`` guard gives the re-ask
rhythm for free. Confirms and snoozes are plain coaching-state cursors
(``*_count`` = ``date|n`` toward the target, ``*_snoozed_until``), no schema
change. Every confirm/snooze also logs a ``self_care`` episode so a later
learning pass can adapt cadence (see ROADMAP — including the honesty check on
reflexive instant-yeses).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from prefrontal.clock import TS_FMT, local_datetime
from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.coaching import DEFAULT_RESPONSIVE_END, CoachContext, Cue, last_fired
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Meal-check defaults (target 1: one "Ate" and it's done for the day).
DEFAULT_MEAL_START_HOUR = 11
DEFAULT_MEAL_REASK_MINUTES = 40
DEFAULT_MEAL_SNOOZE_MINUTES = 30
DEFAULT_MEAL_DAILY_TARGET = 1
#: Water-check defaults (recurring toward a daily target of glasses).
DEFAULT_WATER_START_HOUR = 9
DEFAULT_WATER_INTERVAL_MINUTES = 90
DEFAULT_WATER_SNOOZE_MINUTES = 30
DEFAULT_WATER_DAILY_TARGET = 6
#: Meds-check defaults (target 1: one "Took" and it's done for the day). More
#: sensitive than eat/drink, so it's *off* even when self_care is on — opt in per
#: person via ``meds_enabled``; a multi-dose regimen just raises ``meds_daily_target``.
DEFAULT_MEDS_START_HOUR = 9
DEFAULT_MEDS_REASK_MINUTES = 30
DEFAULT_MEDS_SNOOZE_MINUTES = 30
DEFAULT_MEDS_DAILY_TARGET = 1
#: Bio-break defaults (recurring within a time *window*: every 2h from 8:00,
#: ending at 19:00). The first check bounded by an **end hour** — it stops at
#: ``biobreak_end_hour`` regardless of the daily total, since a bathroom-break
#: reminder past the evening is just noise. On with self_care (like meal/water);
#: turn off per person via ``biobreak_enabled``.
DEFAULT_BIOBREAK_START_HOUR = 8
DEFAULT_BIOBREAK_END_HOUR = 19
DEFAULT_BIOBREAK_INTERVAL_MINUTES = 120
DEFAULT_BIOBREAK_SNOOZE_MINUTES = 30
#: Bio breaks are open-ended (not a quota), so this is only an informational
#: tally cap — the end hour, never a daily total, is what stops the reminders. It
#: no longer drives a "n/6" dashboard readout (that goal display was dropped when
#: the check became open-ended); it just bounds the count kept for the record.
DEFAULT_BIOBREAK_DAILY_TARGET = 6
#: Wind-down defaults (target 1: one "Winding down" and it's settled for the night).
#: Unlike the other basics this one lives in the *evening* — it nudges in the
#: run-up to bed, so its default start hour sits just inside the default
#: responsive-hours end (22:00). The engine still owns the "no overnight nag"
#: guarantee: a wind-down cue outside responsive hours is suppressed like any
#: other, so the check leans on that gate rather than re-implementing a bedtime.
#: It's the personal-preference kind of nudge (a bedtime is opinionated), so like
#: meds it's **off even when self_care is on** — opt in via ``winddown_enabled``.
DEFAULT_WINDDOWN_START_HOUR = 21
DEFAULT_WINDDOWN_REASK_MINUTES = 30
DEFAULT_WINDDOWN_SNOOZE_MINUTES = 15
DEFAULT_WINDDOWN_DAILY_TARGET = 1
#: Movement/stretch defaults (target 1: one "Stretched" and it's done for the day).
#: The daily-movement *floor* — a tiny, always-doable minimum (a few minutes of
#: stretching) that survives the worst day, so a movement habit never hits zero.
#: Like meds/winddown it's the personal-preference kind of nudge (an exercise
#: cadence is personal), so it's **off even when self_care is on** — opt in via
#: ``movement_enabled``. It defaults to the *morning*, anchored to the first
#: coffee: an existing daily pause that happens regardless of how the day goes,
#: early enough that the whole day is still ahead (a stretch sets the tone rather
#: than being the thing you're too fried to do at night), and already a
#: stand-and-wait moment — habit-stack the stretch onto the brew / first cup.
#: Target 1 with a re-ask until met, so blowing past the exact minute doesn't lose
#: the day.
#:
#: Note the interplay with responsive hours (:data:`~prefrontal.coaching.
#: DEFAULT_RESPONSIVE_START`, default 8): a self-care cue is gated by quiet hours
#: like any non-critical nudge, so with the default 7:00 start a user whose
#: responsive window opens at 8 first *receives* it at 8 (it keeps re-asking until
#: then). To actually land it at a 7:00 coffee, widen ``responsive_hours_start``
#: to 7 — a deliberate choice, since it opens the whole nudge window earlier.
DEFAULT_MOVEMENT_START_HOUR = 7
DEFAULT_MOVEMENT_REASK_MINUTES = 45
DEFAULT_MOVEMENT_SNOOZE_MINUTES = 30
DEFAULT_MOVEMENT_DAILY_TARGET = 1

#: Meal snooze cursor (UTC "YYYY-MM-DD HH:MM:SS"), kept for external references.
SNOOZED_UNTIL_KEY = "meal_snoozed_until"
#: Episode type logged on each self-care response (seeds cadence learning).
SELF_CARE_EPISODE = "self_care"

# -- adaptive cadence (learning §6) ------------------------------------------
#: A confirm this fast after the nudge reads as a reflexive *dismissal*, not a
#: genuine "I did it" — the honesty check. Such taps must not be counted as
#: engagement that justifies backing the cadence off.
INSTANT_CONFIRM_SECONDS = 30.0
#: Minutes an un-acted self-care nudge waits before it's counted "ignored" — a
#: "wrong time / too frequent" signal for the learner. Kept below the shortest
#: default interval so a stale prompt is swept before the next one fires.
SELF_CARE_ACK_WINDOW_MINUTES = 30.0
#: Recent responses to weigh when adapting a check's interval, and the minimum
#: before we adapt at all.
ADAPT_LOOKBACK = 30
MIN_ADAPT_RESPONSES = 5
#: Snooze rate at/above which the nudge is too frequent → widen the interval.
SNOOZE_WIDEN_RATE = 0.4
#: Snooze rate at/below which (with genuine confirms) we ease off slightly.
EASE_OFF_SNOOZE_RATE = 0.1
#: Share of *timed* confirms that are instant, at/above which the honesty check
#: fires and blocks easing off (the taps look like dismissals).
INSTANT_GUARD_RATE = 0.5
#: Minimum genuinely-timed confirms before an ease-off is trusted.
MIN_GENUINE_CONFIRMS = 3
#: How far a learned interval may drift from the check's default (never below it
#: in v1 — we only ever nudge *less*, never quietly more).
MAX_INTERVAL_FACTOR = 3.0
WIDEN_FACTOR = 1.25
EASE_OFF_FACTOR = 1.1


def _prompt_key(check_key: str) -> str:
    """Coaching-state cursor holding when this check last nudged (for latency)."""
    return f"{check_key}_prompt_at"


def meal_message(name: str = "") -> str:
    """The "have you eaten?" nudge text, greeting by name when we have one."""
    lead = f"{name}, quick check" if name else "Quick check"
    return (
        f"{lead} — have you eaten anything yet today? Even a snack counts. "
        "Tap Ate once you have."
    )


def water_message(name: str = "") -> str:
    """The "drink some water" nudge text, greeting by name when we have one."""
    lead = f"{name}, hydration check" if name else "Hydration check"
    return f"{lead} — time for some water. 💧 Tap Drank once you have."


def meds_message(name: str = "") -> str:
    """The "taken your meds?" nudge text, greeting by name when we have one."""
    lead = f"{name}, meds check" if name else "Meds check"
    return f"{lead} — have you taken your meds? 💊 Tap Took once you have."


def biobreak_message(name: str = "") -> str:
    """The "take a bio break" nudge text, greeting by name when we have one."""
    lead = f"{name}, quick break" if name else "Quick break"
    return (
        f"{lead} — time to get up and take a bathroom break. 🚻 "
        "Tap Went once you have."
    )


def winddown_message(name: str = "") -> str:
    """The "start winding down" nudge text, greeting by name when we have one."""
    lead = f"{name}, wind-down check" if name else "Wind-down check"
    return (
        f"{lead} — it's getting late. Time to start winding down for bed. 🌙 "
        "Tap Winding down once you begin."
    )


def movement_message(name: str = "") -> str:
    """The "time to move/stretch" nudge text, greeting by name when we have one."""
    lead = f"{name}, movement check" if name else "Movement check"
    return (
        f"{lead} — time to move. 🧘 Even 3 minutes of stretching counts. "
        "Tap Stretched once you have."
    )


@dataclass(frozen=True)
class BasicCheck:
    """One basic-needs check, declared by its config keys and daily target.

    The **target** is the unifying knob: a once-a-day check (meal) is just target
    1; a recurring one (water) is target N. A confirm counts one toward the day's
    target and — until the target is met — defers the next reminder a full
    interval; meeting the target ends the check for the day. Adding a new basic
    (meds, sleep) is one more entry here plus its ntfy buttons.

    An **open-ended** check (``open_ended=True``, bio breaks) breaks that mold: it
    isn't a quota. There's nothing to "finish" — it just reminds on its interval
    within the window and tracks how you respond. No count ever silences it (only
    the end hour and responsive hours bound it), a confirm just defers the next
    reminder a full interval, and it's never "done for the day". Its ``target`` is
    ignored for gating; the daily count is kept only as an informational tally.
    """

    key: str                # context_key + ntfy button kind: "meal" | "water"
    intervention: str       # declared Intervention.name
    enabled_key: str        # per-check on/off (under the master self_care switch)
    count_key: str          # daily-progress cursor: "YYYY-MM-DD|n"
    target_key: str
    target_default: int
    snooze_key: str         # cursor: UTC ts we're held off until
    start_hour_key: str
    start_hour_default: int
    interval_key: str
    interval_default: int
    snooze_minutes_key: str
    snooze_minutes_default: int
    message: Callable[[str], str]
    confirm_action: str     # NUDGE_ACTIONS name for the confirm button
    snooze_action: str      # NUDGE_ACTIONS name for the snooze button
    progress_headline: str  # confirm that isn't yet the target ({count}/{target})
    done_headline: str      # confirm that meets the target ({count}/{target})
    #: Local hour after which the check goes quiet for the day (``None`` = no end
    #: hour, so only the daily target and responsive hours bound it). A check with
    #: an end hour stops nudging at it regardless of whether the target was met.
    end_hour_key: str | None = None
    end_hour_default: int | None = None
    #: When ``True`` this check is a pure interval reminder, not a quota: no daily
    #: count silences it (only its end hour / responsive hours do), a confirm just
    #: defers the next reminder, and it's never "done for the day". The ``target``
    #: is ignored for gating; the count is kept as an informational tally only.
    open_ended: bool = False
    #: Open-ended checks only: the state key holding the UTC timestamp a *confirm*
    #: keeps the check "satisfied" until — set on Went (not on Snooze), so a surface
    #: can show the check green after you've been and clear it the moment the next
    #: reminder comes due. ``None`` for quota checks, whose green state is ``done``.
    confirmed_key: str | None = None


CHECKS: tuple[BasicCheck, ...] = (
    BasicCheck(
        key="meal",
        intervention="meal_check",
        enabled_key="meal_enabled",
        count_key="meal_count",
        target_key="meal_daily_target",
        target_default=DEFAULT_MEAL_DAILY_TARGET,
        snooze_key=SNOOZED_UNTIL_KEY,
        start_hour_key="meal_start_hour",
        start_hour_default=DEFAULT_MEAL_START_HOUR,
        interval_key="meal_reask_minutes",
        interval_default=DEFAULT_MEAL_REASK_MINUTES,
        snooze_minutes_key="meal_snooze_minutes",
        snooze_minutes_default=DEFAULT_MEAL_SNOOZE_MINUTES,
        message=meal_message,
        confirm_action="meal_ate",
        snooze_action="meal_snooze",
        progress_headline="Nice — that's {count} today. 🍽️",
        done_headline="Nice — glad you ate. 🍽️ I'll leave you be today.",
    ),
    BasicCheck(
        key="water",
        intervention="water_check",
        enabled_key="water_enabled",
        count_key="water_count",
        target_key="water_daily_target",
        target_default=DEFAULT_WATER_DAILY_TARGET,
        snooze_key="water_snoozed_until",
        start_hour_key="water_start_hour",
        start_hour_default=DEFAULT_WATER_START_HOUR,
        interval_key="water_interval_minutes",
        interval_default=DEFAULT_WATER_INTERVAL_MINUTES,
        snooze_minutes_key="water_snooze_minutes",
        snooze_minutes_default=DEFAULT_WATER_SNOOZE_MINUTES,
        message=water_message,
        confirm_action="water_drank",
        snooze_action="water_snooze",
        progress_headline="Nice — {count}/{target} today. 💧 I'll remind you again in a bit.",
        done_headline="That's all {target} for today — nicely done. 💧",
    ),
    BasicCheck(
        key="meds",
        intervention="meds_check",
        enabled_key="meds_enabled",
        count_key="meds_count",
        target_key="meds_daily_target",
        target_default=DEFAULT_MEDS_DAILY_TARGET,
        snooze_key="meds_snoozed_until",
        start_hour_key="meds_start_hour",
        start_hour_default=DEFAULT_MEDS_START_HOUR,
        interval_key="meds_reask_minutes",
        interval_default=DEFAULT_MEDS_REASK_MINUTES,
        snooze_minutes_key="meds_snooze_minutes",
        snooze_minutes_default=DEFAULT_MEDS_SNOOZE_MINUTES,
        message=meds_message,
        confirm_action="meds_took",
        snooze_action="meds_snooze",
        progress_headline="Got it — {count}/{target} today. 💊",
        done_headline="Meds done for today. 💊 Nice.",
    ),
    BasicCheck(
        key="biobreak",
        intervention="biobreak_check",
        enabled_key="biobreak_enabled",
        count_key="biobreak_count",
        target_key="biobreak_daily_target",
        target_default=DEFAULT_BIOBREAK_DAILY_TARGET,
        snooze_key="biobreak_snoozed_until",
        start_hour_key="biobreak_start_hour",
        start_hour_default=DEFAULT_BIOBREAK_START_HOUR,
        interval_key="biobreak_interval_minutes",
        interval_default=DEFAULT_BIOBREAK_INTERVAL_MINUTES,
        snooze_minutes_key="biobreak_snooze_minutes",
        snooze_minutes_default=DEFAULT_BIOBREAK_SNOOZE_MINUTES,
        message=biobreak_message,
        confirm_action="biobreak_went",
        snooze_action="biobreak_snooze",
        # Open-ended: not a quota, so the confirm copy doesn't count toward a goal
        # (done_headline is never shown — it's never "done for the day").
        progress_headline="Good — I'll check back in a bit. 🚻",
        done_headline="Good — I'll check back in a bit. 🚻",
        end_hour_key="biobreak_end_hour",
        end_hour_default=DEFAULT_BIOBREAK_END_HOUR,
        open_ended=True,
        confirmed_key="biobreak_confirmed_until",
    ),
    BasicCheck(
        key="winddown",
        intervention="winddown_check",
        enabled_key="winddown_enabled",
        count_key="winddown_count",
        target_key="winddown_daily_target",
        target_default=DEFAULT_WINDDOWN_DAILY_TARGET,
        snooze_key="winddown_snoozed_until",
        start_hour_key="winddown_start_hour",
        start_hour_default=DEFAULT_WINDDOWN_START_HOUR,
        interval_key="winddown_reask_minutes",
        interval_default=DEFAULT_WINDDOWN_REASK_MINUTES,
        snooze_minutes_key="winddown_snooze_minutes",
        snooze_minutes_default=DEFAULT_WINDDOWN_SNOOZE_MINUTES,
        message=winddown_message,
        confirm_action="winddown_started",
        snooze_action="winddown_snooze",
        # Target 1, so the first confirm meets it and shows done_headline;
        # progress_headline is a belt-and-braces default it never actually reaches.
        progress_headline="Good — winding down. 🌙",
        done_headline="Good — wind down and rest well. 🌙 Night.",
    ),
    BasicCheck(
        key="movement",
        intervention="movement_check",
        enabled_key="movement_enabled",
        count_key="movement_count",
        target_key="movement_daily_target",
        target_default=DEFAULT_MOVEMENT_DAILY_TARGET,
        snooze_key="movement_snoozed_until",
        start_hour_key="movement_start_hour",
        start_hour_default=DEFAULT_MOVEMENT_START_HOUR,
        interval_key="movement_reask_minutes",
        interval_default=DEFAULT_MOVEMENT_REASK_MINUTES,
        snooze_minutes_key="movement_snooze_minutes",
        snooze_minutes_default=DEFAULT_MOVEMENT_SNOOZE_MINUTES,
        message=movement_message,
        confirm_action="movement_stretched",
        snooze_action="movement_snooze",
        # Target 1, like the meal/wind-down once-a-day checks: the first confirm
        # meets it and shows done_headline; progress_headline is never reached.
        progress_headline="Nice — that's some movement in. 🧘",
        done_headline="Nice — that's your movement floor for today. 🧘 Well done.",
    ),
)

#: One-tap action name → the check it resolves (built from CHECKS).
_ACTION_CHECK: dict[str, BasicCheck] = {}
for _c in CHECKS:
    _ACTION_CHECK[_c.confirm_action] = _c
    _ACTION_CHECK[_c.snooze_action] = _c

#: The full set of self-care one-tap actions, for the /nudge/act dispatcher.
SELF_CARE_ACTIONS = frozenset(_ACTION_CHECK)


def _stamp(now: datetime, minutes: int) -> str:
    return (now + timedelta(minutes=minutes)).strftime(TS_FMT)


def _target(store: MemoryStore, check: BasicCheck) -> int:
    return max(1, int(store.get_float(check.target_key, check.target_default)))


def _end_hour(store: MemoryStore, check: BasicCheck) -> int | None:
    """The local hour after which ``check`` goes quiet, or ``None`` if it has none.

    Only checks that declare an ``end_hour_key`` (bio breaks today) are bounded by
    a wall-clock end; the rest run until their daily target or responsive hours.
    """
    if check.end_hour_key is None:
        return None
    return store.get_hour(check.end_hour_key, check.end_hour_default or 24)


def day_count(store: MemoryStore, check: BasicCheck, today: str) -> int:
    """How many confirms the user has logged for ``check`` *today* (0 if none).

    The cursor is ``"YYYY-MM-DD|n"``; a date mismatch means a new day, so the
    count resets to 0 without a nightly job.
    """
    raw = store.get_state(check.count_key)
    if not raw:
        return 0
    date, _, n = str(raw).partition("|")
    if date != today:
        return 0
    try:
        return int(n)
    except ValueError:
        return 0


def _is_overdue(
    *,
    enabled: bool,
    done: bool,
    target: int,
    count: int,
    start_hour: int,
    interval: int,
    local: datetime,
    end_hour: int | None = None,
) -> bool:
    """Whether a check is "behind" right now — the flag a surface flashes on (pure).

    Never overdue when disabled, already met, before the start hour, or (for a
    window-bounded check) past its end hour. A once-a-day check (``target <= 1``)
    is overdue simply once past its start hour and not done. A recurring check is
    *pace-aware*: since its start hour you'd expect about one confirm per
    ``interval`` minutes, so it's overdue only when ``count`` has fallen below that
    running expectation — so water flags when you've genuinely lagged the pace, not
    merely because you're not yet at the daily total. Expectation is capped at
    ``target`` (you're never "behind" once you've done enough).
    """
    if not enabled or done or local.hour < start_hour:
        return False
    if end_hour is not None and local.hour >= end_hour:
        return False  # past the check's window — it's gone quiet, not "behind"
    if target <= 1:
        return True
    minutes_since = (local.hour - start_hour) * 60 + local.minute
    expected = min(target, minutes_since // interval)
    return count < expected


def _next_due(
    *,
    enabled: bool,
    done: bool,
    open_ended: bool,
    target: int,
    count: int,
    start_hour: int,
    end_hour: int | None,
    interval: int,
    local: datetime,
    responsive_end: int,
) -> str | None:
    """The next *future* local instant this check wants a nudge, or ``None``.

    Returned as UTC ``TS_FMT`` text — the same format the iOS app parses — so the
    app can schedule an **offline-tolerant local notification** against it (#474),
    a self-care companion to the departure "leave by" local nudge. Mirrors the
    pace model in :func:`_is_overdue`: a once-a-day check is due at its start hour;
    a recurring quota paces one nudge per ``interval`` minutes from the start hour.

    Only a **future** time inside the check's window is returned — a local
    notification can't fire in the past, and nothing is scheduled at/after the
    window end (``end_hour``, else the responsive-hours end) so there are no
    overnight buzzes. Off, already-``done``, and open-ended checks return ``None``
    (an open-ended reminder has no fixed schedule — it's an interval since your
    last action, not a clock time). Note ``count`` is unused for now but kept so the
    signature reads as "everything the pace depends on."
    """
    if not enabled or done or open_ended:
        return None
    window_end = end_hour if end_hour is not None else responsive_end
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if target <= 1 or local <= start:
        due = start
    else:
        minutes_since = (local - start).total_seconds() / 60.0
        steps = int(minutes_since // interval) + 1
        due = start + timedelta(minutes=steps * interval)
    if due <= local or due.hour >= window_end:
        return None
    return due.astimezone(timezone.utc).replace(tzinfo=None).strftime(TS_FMT)


def self_care_status(store: MemoryStore, now: datetime, tz: str) -> dict[str, Any]:
    """Today's self-care state for the read-only surfaces (the dashboard card).

    A pure read that mirrors what :meth:`SelfCareModule.evaluate` gates on, so a
    surface can show *why* a check is or isn't nudging: the ``enabled`` master
    switch, and per check whether it's individually enabled, today's progress
    toward its target, whether that target is already met, whether it's ``overdue``
    (past its start hour today and still unmet), and the effective start hour +
    cadence. No side effects — it never fires or records anything.

    The whole point is visibility: a silently-``off`` master switch (the common
    "why aren't I getting nudges?" cause) shows up as ``enabled: false`` rather
    than as nothing at all.
    """
    local = local_datetime(now, tz)
    today = local.strftime("%Y-%m-%d")
    responsive_end = store.get_hour("responsive_hours_end", DEFAULT_RESPONSIVE_END)
    checks: list[dict[str, Any]] = []
    for check in CHECKS:
        count = day_count(store, check, today)
        target = _target(store, check)
        enabled = (store.get_state(check.enabled_key, "on") or "on") == "on"
        # An open-ended check (bio breaks) is never "done" — it's a reminder, not a
        # quota — so the count can't complete it.
        done = (not check.open_ended) and count >= target
        start_hour = store.get_hour(check.start_hour_key, check.start_hour_default)
        end_hour = _end_hour(store, check)
        interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
        satisfied = False
        if check.open_ended:
            # "Behind" doesn't mean off-pace-vs-quota here; it means a reminder is
            # due right now — within the window, past the start hour, and not
            # currently deferred by a recent Went/Snooze. So the dashboard flashes
            # exactly when there's a nudge to respond to, and clears for an interval
            # after you tap Went.
            snoozed_until = _parse_ts(store.get_state(check.snooze_key))
            within = local.hour >= start_hour and (end_hour is None or local.hour < end_hour)
            overdue = enabled and within and (snoozed_until is None or now >= snoozed_until)
            # "Satisfied" is the open-ended analog of a quota check's ``done``: you
            # confirmed (Went) and the next reminder isn't due yet, so a surface shows
            # it green until that interval elapses. Set only by a confirm, so a snooze
            # doesn't green it. Mutually exclusive with overdue (a confirm pushes
            # ``snoozed_until`` to the same instant, so it can't be "due" while green).
            confirmed_until = (
                _parse_ts(store.get_state(check.confirmed_key)) if check.confirmed_key else None
            )
            satisfied = confirmed_until is not None and now < confirmed_until
        else:
            overdue = _is_overdue(
                enabled=enabled, done=done, target=target, count=count,
                start_hour=start_hour, interval=interval, local=local,
                end_hour=end_hour,
            )
        checks.append(
            {
                "key": check.key,
                "enabled": enabled,
                "count": count,
                "target": target,
                "done": done,
                # A reminder, not a quota: surfaces render count without a "/target"
                # goal and never show a "done ✓" for it.
                "open_ended": check.open_ended,
                # Open-ended only: you've confirmed (Went) and aren't due again yet,
                # so a surface shows it green. Always false for a quota check (its
                # green is ``done``). Clears when the next reminder comes due.
                "satisfied": satisfied,
                "start_hour": start_hour,
                # Local hour the check stops for the day, or None when it's bounded
                # only by its target + responsive hours (a surface renders the window
                # as "from 8:00" vs "8:00–19:00").
                "end_hour": end_hour,
                # "You're behind on this" — the flag a surface flashes on (computed
                # above). Always false when done, disabled, or before the start hour.
                # For a once-a-day check (target 1) that's simply "past the start
                # hour and still not done". For a recurring quota (water) it's
                # *pace-aware*: you'd expect roughly one per interval since the start
                # hour, so it flags only when the count has fallen short of that
                # running expectation — it clears as you catch up, instead of nagging
                # all day just because you're not yet at the daily total. For an
                # open-ended check (bio breaks) it means a reminder is due right now.
                "overdue": overdue,
                "interval_minutes": interval,
                # The next future local time this check wants a nudge (UTC text),
                # so the iOS app can schedule an offline local notification for it
                # (#474). None when off/done/open-ended or nothing's left today.
                "next_due": _next_due(
                    enabled=enabled, done=done, open_ended=check.open_ended,
                    target=target, count=count, start_hour=start_hour,
                    end_hour=end_hour, interval=interval, local=local,
                    responsive_end=responsive_end,
                ),
            }
        )
    return {
        "enabled": (store.get_state("self_care", "off") or "off") == "on",
        "checks": checks,
    }


def apply_self_care_config(
    store: MemoryStore,
    *,
    enabled: bool | None = None,
    checks: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Persist self-care settings from the dashboard (explicit user writes).

    The write twin of :func:`self_care_status`: a partial update that touches
    only the fields present, mapping each check's knobs onto the coaching-state
    keys its own :class:`BasicCheck` declares (so there's no second copy of the
    key names). Every write is ``source="explicit"`` — a value the user set by
    hand, which the nightly cadence learner then leaves alone.

    Args:
        store: A user-scoped store.
        enabled: Master switch, when provided.
        checks: ``{check_key: {enabled?, target?, start_hour?, end_hour?,
            interval_minutes?}}``. ``end_hour`` applies only to a window-bounded
            check (bio breaks); it's ignored for the rest. Unknown check keys are
            ignored; absent fields are left untouched.
    """
    if enabled is not None:
        store.set_state("self_care", "on" if enabled else "off", source="explicit")
    by_key = {c.key: c for c in CHECKS}
    for key, cfg in (checks or {}).items():
        check = by_key.get(key)
        if check is None:
            continue  # unknown check key — ignore rather than error
        if cfg.get("enabled") is not None:
            store.set_state(
                check.enabled_key, "on" if cfg["enabled"] else "off", source="explicit"
            )
        if cfg.get("target") is not None and not check.open_ended:
            # An open-ended check has no quota to set — ignore a stray target write.
            store.set_state(check.target_key, str(max(1, int(cfg["target"]))), source="explicit")
        if cfg.get("start_hour") is not None:
            hour = str(min(23, max(0, int(cfg["start_hour"]))))
            store.set_state(check.start_hour_key, hour, source="explicit")
        if cfg.get("end_hour") is not None and check.end_hour_key is not None:
            # Only meaningful for a window-bounded check (bio breaks); ignored for
            # the rest, which have no end-hour key to write.
            hour = str(min(23, max(0, int(cfg["end_hour"]))))
            store.set_state(check.end_hour_key, hour, source="explicit")
        if cfg.get("interval_minutes") is not None:
            store.set_state(
                check.interval_key, str(max(1, int(cfg["interval_minutes"]))), source="explicit"
            )


def apply_self_care_action(
    store: MemoryStore, action: str, *, now: datetime, today: str
) -> str | None:
    """Apply a one-tap self-care action; return the confirmation copy (or ``None``).

    Centralizes confirm/snooze semantics so ``/nudge/act`` stays thin:

    - **confirm** counts one toward the day's target and logs a ``self_care``
      episode; if the target isn't met yet it defers the next reminder a full
      interval, otherwise the check is done for the day.
    - **snooze** defers by the check's snooze minutes and logs the response.

    Returns ``None`` for an unknown action (so the caller can fall through).
    """
    check = _ACTION_CHECK.get(action)
    if check is None:
        return None
    # Response latency (nudge → this tap) is the honesty-check signal: read and
    # clear the prompt stamp so it can't be reused by a later tap.
    lat = _latency_note(store, check, now)
    if action == check.confirm_action:
        count = day_count(store, check, today) + 1
        store.set_state(check.count_key, f"{today}|{count}", source="explicit")
        target = _target(store, check)
        # An open-ended check logs the tally but has no target to progress toward.
        notes = f"{count} {lat}" if check.open_ended else f"{count}/{target} {lat}"
        store.log_episode(
            SELF_CARE_EPISODE,
            acknowledged=True,
            context=f"{check.key}: confirmed",
            outcome="confirmed",
            notes=notes,
        )
        # A quota check ends for the day once met; an open-ended one never does —
        # it just spaces the next reminder a full interval out, same as a
        # not-yet-met quota confirm.
        if not check.open_ended and count >= target:
            return check.done_headline.format(count=count, target=target)
        interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
        store.set_state(check.snooze_key, _stamp(now, interval), source="explicit")
        # For an open-ended check, a confirm also marks it "satisfied" until the
        # next reminder is due, so the dashboard shows it green after you've been
        # (a Snooze, below, deliberately doesn't set this — snoozing isn't going).
        if check.confirmed_key:
            store.set_state(check.confirmed_key, _stamp(now, interval), source="explicit")
        return check.progress_headline.format(count=count, target=target)
    # snooze
    mins = int(store.get_float(check.snooze_minutes_key, check.snooze_minutes_default))
    store.set_state(check.snooze_key, _stamp(now, mins), source="explicit")
    store.log_episode(
        SELF_CARE_EPISODE,
        acknowledged=True,
        context=f"{check.key}: snoozed",
        outcome="snoozed",
        notes=f"{mins}m {lat}",
    )
    return f"Okay — I'll check back in {mins} min."


def apply_self_care_mark(
    store: MemoryStore, key: str, *, now: datetime, today: str
) -> str | None:
    """Log a self-care confirm *by check key* from an authenticated surface.

    The dashboard's "mark what I did today" buttons need to record a confirm
    without the signed one-tap token that ``/nudge/act`` carries — a signed-in
    user is already authenticated. This resolves the check by its ``key``
    (``meal`` / ``water`` / ``meds``) and defers to
    :func:`apply_self_care_action`, so the count/target and episode-logging
    semantics stay identical to a notification tap.

    Returns the confirmation copy, or ``None`` for an unknown key.
    """
    check = next((c for c in CHECKS if c.key == key), None)
    if check is None:
        return None
    return apply_self_care_action(store, check.confirm_action, now=now, today=today)


def apply_self_care_unmark(store: MemoryStore, key: str, *, today: str) -> str | None:
    """Reduce a check's logged count for *today* by one, flooring at zero.

    Backs the dashboard chip's shift-click mis-tap correction ("I logged that by
    accident"). It rewinds only the day counter — the chip's ``count``/``target``
    readout and its done-for-the-day state both derive from it — so a decrement
    below target flips the check back to not-done. The historical ``self_care``
    episodes are an append-only log (Insights reads them) and are deliberately
    left untouched. Returns confirmation copy, or ``None`` for an unknown key.
    """
    check = next((c for c in CHECKS if c.key == key), None)
    if check is None:
        return None
    count = max(0, day_count(store, check, today) - 1)
    store.set_state(check.count_key, f"{today}|{count}", source="explicit")
    # Removing the last open-ended log clears its "satisfied" (green) marker too, so
    # a mis-tap correction un-greens the chip rather than leaving it green on a zero
    # tally. (A quota check un-greens via its count/target readout instead.)
    if check.open_ended and check.confirmed_key and count == 0:
        store.set_state(check.confirmed_key, "", source="explicit")
    return f"Removed one — {count}/{_target(store, check)} today."


def apply_self_care_reset(store: MemoryStore, key: str, *, today: str) -> str | None:
    """Reset a check's day count to zero — the chip's tap-at-max wrap-around.

    On mobile there's no shift-click to rewind a mis-tap, so once a chip reaches
    its daily target a further tap cycles it back to zero (this) rather than
    overshooting or doing nothing — the counter reads as ``0…target`` and wraps.
    Like :func:`apply_self_care_unmark` it only rewinds the day counter (flipping
    the check back to not-done, so it can nudge again); the append-only
    ``self_care`` episodes Insights reads are left untouched. Returns confirmation
    copy, or ``None`` for an unknown key.
    """
    check = next((c for c in CHECKS if c.key == key), None)
    if check is None:
        return None
    store.set_state(check.count_key, f"{today}|0", source="explicit")
    return f"Reset — 0/{_target(store, check)} today."


def _latency_note(store: MemoryStore, check: BasicCheck, now: datetime) -> str:
    """Pop the prompt stamp and render the nudge→tap latency as a note token.

    Returns ``"latency=<n>s"`` when we know when the nudge fired, else
    ``"latency=?"`` (e.g. a tap with no tracked prompt, or a direct API call).
    Clearing the stamp means a second tap can't reuse the same delivery time.
    """
    prompted = _parse_ts(store.get_state(_prompt_key(check.key)))
    store.set_state(_prompt_key(check.key), "", source="inferred")
    if prompted is None:
        return "latency=?"
    seconds = max(0, int((now - prompted).total_seconds()))
    return f"latency={seconds}s"


def mark_self_care_prompted(store: MemoryStore, decisions: list[Any], now: datetime) -> None:
    """Stamp the delivery time for each self-care cue just fired (for latency).

    Called by the coaching tick right after it commits to firing (``record_fired``)
    — from both ``/webhooks/coach/check`` and ``prefrontal coach --deliver`` — so
    the next Ate/Drank/Snooze tap can be timed against when the nudge actually
    went out. A no-op for non-self-care decisions.
    """
    stamp = now.strftime(TS_FMT)
    for d in decisions:
        cue = getattr(d, "cue", None)
        if cue is not None and cue.module == "self_care":
            store.set_state(_prompt_key(cue.context_key), stamp, source="inferred")


def sweep_unanswered_self_care(
    store: MemoryStore,
    now: datetime,
    *,
    window_minutes: float = SELF_CARE_ACK_WINDOW_MINUTES,
) -> int:
    """Count self-care nudges left un-acted past the window as ``ignored``, and clear them.

    A meal/water cue stamps its delivery time (:func:`mark_self_care_prompted`);
    an Ate/Drank/Snooze tap clears it (:func:`_latency_note`). Anything still
    stamped past ``window_minutes`` was neither confirmed nor snoozed — an
    *unanswered* nudge. Logging it as an ``ignored`` ``self_care`` episode gives
    :func:`adapt_self_care_interval` the "wrong time / too frequent" signal that
    snoozes alone miss (many people ignore a nudge rather than tapping snooze).

    Mirrors :func:`prefrontal.coaching.sweep_stale_nudges`: run once per coaching
    tick, before new prompts are stamped. Returns how many were swept.
    """
    swept = 0
    for check in CHECKS:
        stamped = _parse_ts(store.get_state(_prompt_key(check.key)))
        if stamped is None:
            continue
        if (now - stamped).total_seconds() / 60.0 < window_minutes:
            continue  # still inside the ack window — a tap may yet arrive
        store.set_state(_prompt_key(check.key), "", source="inferred")
        store.log_episode(
            SELF_CARE_EPISODE,
            acknowledged=False,
            context=f"{check.key}: ignored",
            outcome="ignored",
            notes="latency=?",
        )
        swept += 1
    return swept


# -- auto-satisfy from other signals -----------------------------------------
#
# The checks are otherwise interval-driven and blind to what the rest of the
# system already knows: a just-returned outing/trip that was a meal (or a calendar
# lunch) means you've eaten; a logged workout means you moved. Cross-reference
# those so a check quietly counts itself met instead of nagging "have you eaten?"
# the minute you walk back in from lunch — the nudges read as attentive, not
# oblivious.

#: Default look-back (minutes) for a satisfying signal — a meal an hour ago still
#: means you've eaten. Tunable via ``self_care_satisfy_window_minutes``.
DEFAULT_SATISFY_WINDOW_MINUTES = 90.0

#: Per-check keyword sets that mark an outing/trip/commitment as *evidence* the
#: need is already met. Only checks with a clear external footprint get one — meal
#: and the movement floor; water/meds/etc. have no such signal.
_SATISFY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "meal": (
        "lunch", "dinner", "breakfast", "brunch", "restaurant", "diner", "cafe",
        "café", "meal", "food", "eat", "eating", "lunched", "dined", "takeout",
    ),
    "movement": (
        "gym", "workout", "work out", "run", "running", "jog", "jogging", "walk",
        "walking", "exercise", "yoga", "pilates", "swim", "swimming", "hike",
        "hiking", "bike", "biking", "cycling", "spin class",
    ),
}
_SATISFY_RES: dict[str, re.Pattern[str]] = {
    key: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b")
    for key, kws in _SATISFY_KEYWORDS.items()
}


def _signal_matches(text: str | None, check_key: str) -> bool:
    """Whether ``text`` names an activity that satisfies ``check_key`` (word-boundary)."""
    pattern = _SATISFY_RES.get(check_key)
    if pattern is None or not text:
        return False
    return pattern.search(text.lower()) is not None


def recently_satisfied(
    store: MemoryStore,
    check_key: str,
    now: datetime,
    *,
    window_minutes: float = DEFAULT_SATISFY_WINDOW_MINUTES,
) -> str | None:
    """A human reason this check is already met by a recent signal, or ``None``.

    Scans, within ``window_minutes``: returned **outings** (their intention),
    completed **trips** (label/category), and past **commitments** (title). The
    first keyword hit wins. Only ``meal``/``movement`` have keyword sets, so any
    other check is never auto-satisfied.
    """
    if check_key not in _SATISFY_KEYWORDS:
        return None
    cutoff = now - timedelta(minutes=window_minutes)

    for outing in store.recent_outings(limit=20):
        if outing.get("status") != "returned":
            continue
        ret = _parse_ts(outing.get("returned_at"))
        if ret is None or ret < cutoff or ret > now:
            continue
        if _signal_matches(outing.get("intention"), check_key):
            return f"a “{outing.get('intention')}” outing"

    for trip in store.recent_trips(limit=20):
        if trip.get("status") != "completed":
            continue
        ret = _parse_ts(trip.get("returned_at"))
        if ret is None or ret < cutoff or ret > now:
            continue
        text = f"{trip.get('label') or ''} {trip.get('category') or ''}"
        if _signal_matches(text, check_key):
            return f"a {(trip.get('label') or trip.get('category'))} trip"

    start = cutoff.strftime(TS_FMT)
    for commit in store.commitments_between(start, now.strftime(TS_FMT)):
        if _signal_matches(commit.get("title"), check_key):
            return f"“{commit.get('title')}” on your calendar"
    return None


def auto_satisfy_from_signals(
    store: MemoryStore, now: datetime, tz: str
) -> list[dict[str, Any]]:
    """Mark a basic-needs check met when a recent signal already covers it.

    Runs each tick (via :meth:`SelfCareModule.before_collect`, before cues are
    collected) so a check that's genuinely handled — you just got back from lunch,
    you logged a run — counts itself done for the day and stops nudging, logging a
    ``confirmed`` ``self_care`` episode noting the evidence. Only meal/movement have
    satisfying signals; a check already met, disabled, or off is skipped. Returns a
    per-satisfied-check summary.
    """
    if (store.get_state("self_care", "off") or "off") != "on":
        return []
    window = store.get_float("self_care_satisfy_window_minutes", DEFAULT_SATISFY_WINDOW_MINUTES)
    today = local_datetime(now, tz).strftime("%Y-%m-%d")
    out: list[dict[str, Any]] = []
    for check in CHECKS:
        if check.key not in _SATISFY_KEYWORDS or check.open_ended:
            continue
        if (store.get_state(check.enabled_key, "on") or "on") == "off":
            continue
        if day_count(store, check, today) >= _target(store, check):
            continue  # already met — nothing to auto-satisfy
        reason = recently_satisfied(store, check.key, now, window_minutes=window)
        if reason is None:
            continue
        target = _target(store, check)
        store.set_state(check.count_key, f"{today}|{target}", source="explicit")
        store.log_episode(
            SELF_CARE_EPISODE,
            acknowledged=True,
            context=f"{check.key}: confirmed",
            outcome="confirmed",
            notes=f"auto-satisfied by {reason}; latency=?",
        )
        out.append({"check": check.key, "reason": reason})
    return out


def _latency_seconds(notes: str | None) -> float | None:
    """Parse ``latency=<n>s`` from an episode's notes, or ``None`` if absent."""
    if not notes:
        return None
    m = re.search(r"latency=(\d+)s", notes)
    return float(m.group(1)) if m else None


def _recent_responses(store: MemoryStore, check: BasicCheck) -> list[dict[str, Any]]:
    """This check's recent self-care responses (newest first), parsed for learning.

    Each item is ``{"outcome": "confirmed"|"snoozed", "latency": float|None}``.
    """
    prefix = f"{check.key}: "
    out: list[dict[str, Any]] = []
    for e in store.episodes_by_type(SELF_CARE_EPISODE, limit=ADAPT_LOOKBACK * 3):
        if not str(e.get("context") or "").startswith(prefix):
            continue
        out.append({"outcome": e.get("outcome"), "latency": _latency_seconds(e.get("notes"))})
        if len(out) >= ADAPT_LOOKBACK:
            break
    return out


def _bounded_interval(value: float, default: int) -> int:
    """Clamp a proposed interval to ``[default, default × MAX_INTERVAL_FACTOR]``,
    rounded to the nearest 5 minutes (we only ever widen in v1)."""
    hi = default * MAX_INTERVAL_FACTOR
    clamped = min(max(value, default), hi)
    return int(round(clamped / 5.0) * 5)


def _resist_rate(responses: list[dict[str, Any]]) -> float:
    """Share of responses that were a "not now" — a snooze or an ignore."""
    if not responses:
        return 0.0
    return sum(1 for r in responses if r["outcome"] in ("snoozed", "ignored")) / len(responses)


def _widen_helped(responses: list[dict[str, Any]]) -> bool:
    """Did an earlier widen actually ease the push-back? (recent vs. older half).

    ``responses`` are newest-first. Splits them in two and asks whether the more
    recent half resisted *less* than the older half — i.e. widening the interval
    correlated with fewer snoozes/ignores. Used to verify a widen before trusting
    it enough to widen *further* (roadmap §6 step 4).
    """
    half = len(responses) // 2
    recent, older = responses[:half], responses[half:]
    return _resist_rate(recent) < _resist_rate(older)


def adapt_self_care_interval(
    responses: list[dict[str, Any]], default_interval: int, *, current_interval: int
) -> tuple[int, str]:
    """Suggest a check's interval from recent responses — with the honesty check.

    v1 only ever *widens* (nudges less), never quietly more:

    - **Resisted a lot** — an explicit **snooze** or an **ignored** (unanswered)
      nudge, both "not now" → widen. The clearest signal. But once we've *already*
      widened, a further widen must be **earned**: if the push-back hasn't eased
      since (recent half no better than older, :func:`_widen_helped`), *hold* —
      don't keep nudging less on the unproven theory that less is better, when the
      last step didn't help (roadmap §6 step 4).
    - **Honesty check:** if the *timed* confirms are mostly **instant** (a
      reflexive yes within :data:`INSTANT_CONFIRM_SECONDS`), *hold* — those taps
      look like dismissals, not genuine "I did it", so they must not justify
      backing off a scaffold that's really just being swatted away.
    - **Genuinely on top of it** (little resistance + enough confirms at a
      plausible latency) → ease off slightly.
    - Otherwise the cadence looks right → hold.

    Returns ``(suggested_minutes, reason)``.
    """
    n = len(responses)
    if n < MIN_ADAPT_RESPONSES:
        return current_interval, "not enough responses yet"
    # "Not now" signals: an explicit snooze *or* an unanswered (ignored) nudge —
    # both say the cadence is too frequent or mistimed.
    resist_rate = _resist_rate(responses)
    if resist_rate >= SNOOZE_WIDEN_RATE:
        # Verify before widening *further*: if we've already widened past the
        # default and the last step didn't ease the push-back, hold rather than
        # compounding a change that isn't helping. The first widen (still at the
        # default) is always allowed; the check needs enough data to split.
        if (
            current_interval > default_interval
            and n >= 2 * MIN_ADAPT_RESPONSES
            and not _widen_helped(responses)
        ):
            return (
                current_interval,
                "already widened but the push-back hasn't eased — holding to see it help",
            )
        widened = _bounded_interval(current_interval * WIDEN_FACTOR, default_interval)
        if widened > current_interval:
            return widened, "you often snooze or ignore these — nudging less frequently"
        return current_interval, "snoozed/ignored often, but already at the max interval"

    timed = [r for r in responses if r["outcome"] == "confirmed" and r["latency"] is not None]
    instant = [r for r in timed if r["latency"] < INSTANT_CONFIRM_SECONDS]
    instant_rate = (len(instant) / len(timed)) if timed else 0.0
    if instant_rate >= INSTANT_GUARD_RATE:
        # Honesty check: reflexive yeses aren't evidence you need it less.
        return current_interval, "frequent instant confirms read as dismissals — keeping presence"
    if (
        resist_rate <= EASE_OFF_SNOOZE_RATE
        and len(timed) >= MIN_GENUINE_CONFIRMS
    ):
        eased = _bounded_interval(current_interval * EASE_OFF_FACTOR, default_interval)
        if eased > current_interval:
            return eased, "consistently handling it — easing off a little"
    return current_interval, "cadence looks about right"


def adapt_self_care(store: MemoryStore, now: datetime | None = None) -> list[dict[str, Any]]:
    """Learn each enabled check's interval from response history (learning §6).

    Runs in the nightly ``learn`` pass. Writes the adapted value to the check's
    own interval key (which the module already reads), so the loop closes with no
    module change — but never overrides an interval the *user* set explicitly.
    Returns a per-check summary for the CLI to print.
    """
    if (store.get_state("self_care", "off") or "off") != "on":
        return []
    state = store.all_state()
    out: list[dict[str, Any]] = []
    for check in CHECKS:
        if (store.get_state(check.enabled_key, "on") or "on") == "off":
            continue
        default = check.interval_default
        current = int(store.get_float(check.interval_key, default))
        suggested, reason = adapt_self_care_interval(
            _recent_responses(store, check), default, current_interval=current
        )
        user_set = (state.get(check.interval_key, {}) or {}).get("source") == "explicit"
        changed = suggested != current and not user_set
        if changed:
            store.set_state(check.interval_key, str(suggested), source="inferred")
        out.append(
            {
                "check": check.key,
                "interval": current if user_set else suggested,
                "changed": changed,
                "reason": "held (you set this interval)" if user_set else reason,
            }
        )
    return out


class SelfCareModule(Module):
    """Nudges the basic needs a focus state quietly overrides.

    Meals, water, meds, sleep (wind-down), and a daily-movement floor.
    """

    key = "self_care"
    title = "Self-Care"
    challenge = (
        "Basic needs — eating, hydrating — dropped during hyperfocus. Not an "
        "executive-function challenge per se but a downstream casualty of one: "
        "the deeper the flow, the easier a meal or a glass of water is to skip."
    )
    # Basic-needs checks are *meant* to break flow, so they pierce protected
    # hyperfocus — the whole point is to interrupt a focus state that has skipped
    # a meal or a glass of water.
    pierces_protection = True
    default_state = {
        # Master switch — off by default, unlike the EF modules, because a
        # self-care nudge is a personal-preference behavior, not a default assist.
        "self_care": "off",
        "meal_enabled": "on",
        "meal_start_hour": str(DEFAULT_MEAL_START_HOUR),
        "meal_reask_minutes": str(DEFAULT_MEAL_REASK_MINUTES),
        "meal_snooze_minutes": str(DEFAULT_MEAL_SNOOZE_MINUTES),
        "meal_daily_target": str(DEFAULT_MEAL_DAILY_TARGET),
        "water_enabled": "on",
        "water_start_hour": str(DEFAULT_WATER_START_HOUR),
        "water_interval_minutes": str(DEFAULT_WATER_INTERVAL_MINUTES),
        "water_snooze_minutes": str(DEFAULT_WATER_SNOOZE_MINUTES),
        "water_daily_target": str(DEFAULT_WATER_DAILY_TARGET),
        # Meds: off even when self_care is on (medication is personal) — opt in
        # explicitly. A multi-dose regimen just raises meds_daily_target.
        "meds_enabled": "off",
        "meds_start_hour": str(DEFAULT_MEDS_START_HOUR),
        "meds_reask_minutes": str(DEFAULT_MEDS_REASK_MINUTES),
        "meds_snooze_minutes": str(DEFAULT_MEDS_SNOOZE_MINUTES),
        "meds_daily_target": str(DEFAULT_MEDS_DAILY_TARGET),
        # Bio breaks: on when self_care is on (like meal/water), but open-ended —
        # a plain interval reminder, not a quota. It nudges within a window and
        # stops at biobreak_end_hour; no daily count silences it. The seeded
        # biobreak_daily_target is kept only as an informational tally. Turn off
        # per person via biobreak_enabled.
        "biobreak_enabled": "on",
        "biobreak_start_hour": str(DEFAULT_BIOBREAK_START_HOUR),
        "biobreak_end_hour": str(DEFAULT_BIOBREAK_END_HOUR),
        "biobreak_interval_minutes": str(DEFAULT_BIOBREAK_INTERVAL_MINUTES),
        "biobreak_snooze_minutes": str(DEFAULT_BIOBREAK_SNOOZE_MINUTES),
        "biobreak_daily_target": str(DEFAULT_BIOBREAK_DAILY_TARGET),
        # Wind-down: off even when self_care is on (a bedtime is a personal
        # preference, like meds) — opt in via winddown_enabled. It nudges in the
        # evening run-up to bed; the engine's responsive hours bound how late.
        "winddown_enabled": "off",
        "winddown_start_hour": str(DEFAULT_WINDDOWN_START_HOUR),
        "winddown_reask_minutes": str(DEFAULT_WINDDOWN_REASK_MINUTES),
        "winddown_snooze_minutes": str(DEFAULT_WINDDOWN_SNOOZE_MINUTES),
        "winddown_daily_target": str(DEFAULT_WINDDOWN_DAILY_TARGET),
        # Movement/stretch: the daily-movement floor. Off even when self_care is
        # on (an exercise cadence is personal, like meds/winddown) — opt in via
        # movement_enabled. Defaults to the morning, anchored to the first coffee;
        # target 1 so one stretch settles it for the day, re-asking until then so a
        # missed moment doesn't lose the day.
        "movement_enabled": "off",
        "movement_start_hour": str(DEFAULT_MOVEMENT_START_HOUR),
        "movement_reask_minutes": str(DEFAULT_MOVEMENT_REASK_MINUTES),
        "movement_snooze_minutes": str(DEFAULT_MOVEMENT_SNOOZE_MINUTES),
        "movement_daily_target": str(DEFAULT_MOVEMENT_DAILY_TARGET),
    }

    def interventions(self) -> list[Intervention]:
        """Declare the basic-needs checks."""
        return [
            Intervention(
                name="meal_check",
                description=(
                    "From meal_start_hour, ask 'have you eaten?' and re-ask every "
                    "meal_reask_minutes until confirmed — even mid-focus. One-tap "
                    "Ate / Snooze on ntfy."
                ),
                trigger="mid-morning onward, until you confirm you've eaten",
                status="active",
            ),
            Intervention(
                name="water_check",
                description=(
                    "From water_start_hour, a 'drink some water' reminder every "
                    "water_interval_minutes until you hit water_daily_target. "
                    "One-tap Drank (counts one, defers an interval) / Snooze on ntfy."
                ),
                trigger="through the day, on an interval, up to the daily target",
                status="active",
            ),
            Intervention(
                name="meds_check",
                description=(
                    "From meds_start_hour, ask 'taken your meds?' and re-ask every "
                    "meds_reask_minutes until you hit meds_daily_target. Off unless "
                    "meds_enabled — medication is personal. One-tap Took / Snooze on ntfy."
                ),
                trigger="from your meds hour, until you confirm the day's dose(s)",
                status="active",
            ),
            Intervention(
                name="biobreak_check",
                description=(
                    "Between biobreak_start_hour and biobreak_end_hour, a 'take a "
                    "bathroom break' reminder every biobreak_interval_minutes. A "
                    "reminder, not a quota: no daily count silences it — the end "
                    "hour bounds it, and Went just defers the next one. On with "
                    "self_care; toggle with biobreak_enabled. One-tap Went / Snooze "
                    "on ntfy."
                ),
                trigger="on an interval through the day, within a set time window",
                status="active",
            ),
            Intervention(
                name="winddown_check",
                description=(
                    "From winddown_start_hour (evening), a 'start winding down for "
                    "bed' reminder every winddown_reask_minutes until you confirm. "
                    "Off unless winddown_enabled — a bedtime is personal. Leans on "
                    "responsive hours so it never nags past your quiet-hours start. "
                    "One-tap Winding down / Snooze on ntfy."
                ),
                trigger="evening run-up to bed, until you confirm you're winding down",
                status="active",
            ),
            Intervention(
                name="movement_check",
                description=(
                    "From movement_start_hour, ask for a few minutes of movement / "
                    "stretching and re-ask every movement_reask_minutes until you "
                    "confirm. The daily-movement floor — a tiny, always-doable "
                    "minimum that survives the worst day. Off unless "
                    "movement_enabled — an exercise cadence is personal; defaults "
                    "to the morning, anchored to the first coffee. One-tap "
                    "Stretched / Snooze on ntfy."
                ),
                trigger="from your movement hour, until you confirm you've moved",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Fire the single most-overdue basic-needs check due right now.

        Opt-in (``self_care`` == ``on``); each check inert before its start hour,
        while snoozed, or once the day's target is met. **At most one cue per tick:**
        several checks coming due together (e.g. meal + water + bio-break all landing
        mid-afternoon) would otherwise fire as a burst of separate notifications —
        the exact pile-up the per-check cadences are tuned to avoid — since debounce
        is per ``dedup_key`` and can't see across checks. So the most-overdue one goes
        now and the rest trickle out on the following ticks. A check already delivered
        for its current window (its bucketed ``dedup_key`` carries a ``coach_fired``
        stamp) is skipped, so capping never lets the just-fired check stay "most
        overdue" and starve the others. The engine adds responsive-hours + debounce
        on top, so nothing nags overnight.
        """
        if (store.get_state("self_care", "off") or "off") != "on":
            return []

        local = local_datetime(ctx.now, ctx.timezone)
        today = local.strftime("%Y-%m-%d")
        minute_of_day = local.hour * 60 + local.minute

        due: list[tuple[int, int, Cue]] = []
        for index, check in enumerate(CHECKS):
            if (store.get_state(check.enabled_key, "on") or "on") == "off":
                continue
            # An open-ended check (bio breaks) is a reminder, not a quota — no count
            # silences it; only its window + snooze/confirm-interval below gate it.
            if not check.open_ended and day_count(store, check, today) >= _target(store, check):
                continue  # hit the day's target already
            snoozed_until = _parse_ts(store.get_state(check.snooze_key))
            if snoozed_until is not None and ctx.now < snoozed_until:
                continue  # deferred (snooze, or the interval after a confirm)

            start_hour = store.get_hour(check.start_hour_key, check.start_hour_default)
            interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
            start_min = start_hour * 60
            if minute_of_day < start_min:
                continue  # too early in the day for this check
            end_hour = _end_hour(store, check)
            if end_hour is not None and local.hour >= end_hour:
                continue  # past this check's window (e.g. no bio-break nudges after 7pm)

            # A fresh bucket each `interval` minutes → a new dedup_key, so each
            # window fires once (the re-ask/repeat rhythm).
            bucket = (minute_of_day - start_min) // interval
            dedup_key = f"self_care:{check.key}:{today}:{bucket}"
            # Already delivered for this window? Skip it — debounce would hold it
            # anyway, and letting it stay "most overdue" would starve the others.
            if last_fired(store, dedup_key) is not None:
                continue
            overdue = minute_of_day - (start_min + bucket * interval)
            due.append((
                overdue,
                index,
                Cue(
                    module=self.key,
                    intervention=check.intervention,
                    urgency="nudge",
                    text=check.message(ctx.display_name),
                    context_key=check.key,
                    dedup_key=dedup_key,
                    # `target` is a synthetic int (the date) so the signed one-tap
                    # button has an id to carry; the tap acts on "now", not on it.
                    ref={"date": today, "target": int(today.replace("-", ""))},
                    suggested_channel="push",
                ),
            ))
        if not due:
            return []
        # Most overdue first; ties fall to CHECKS order (a stable, meaningful priority).
        due.sort(key=lambda t: (-t[0], t[1]))
        return [due[0][2]]

    def before_collect(self, store: MemoryStore, ctx: CoachContext) -> None:
        """Per-tick housekeeping before cues are collected: sweep unanswered nudges
        into ``ignored`` episodes (the "wrong time / too frequent" signal the cadence
        learner reads alongside snoozes), then auto-satisfy any check a recent signal
        already covers (a lunch outing, a logged run) so it doesn't nag redundantly."""
        sweep_unanswered_self_care(store, ctx.now)
        auto_satisfy_from_signals(store, ctx.now, ctx.timezone)

    def after_fire(
        self, store: MemoryStore, decisions: list[Any], ctx: CoachContext
    ) -> None:
        """Stamp the delivery time of any self-care cue that just fired, so a later
        Ate / Drank / Snooze tap can be timed — the honesty-check latency signal
        for adaptive cadence. Filters ``decisions`` to self-care cues itself."""
        mark_self_care_prompted(store, decisions, ctx.now)

    def profile_section(self, store: MemoryStore) -> str | None:
        """Report which basic-needs checks are on."""
        if (store.get_state("self_care", "off") or "off") != "on":
            return None
        lines: list[str] = []
        for check in CHECKS:
            if (store.get_state(check.enabled_key, "on") or "on") == "off":
                continue
            start = store.get_hour(check.start_hour_key, check.start_hour_default)
            end = _end_hour(store, check)
            every = store.get_state(check.interval_key) or str(check.interval_default)
            target = _target(store, check)
            if check.open_ended:
                goal = " (a reminder, not a quota)"
            else:
                goal = "" if target == 1 else f" (up to {target}/day)"
            window = f"from {start}:00" if end is None else f"{start}:00–{end}:00"
            lines.append(
                f"- {check.key.title()} check on: {window}, every {every} min"
                f"{goal} — allowed to interrupt a focus block."
            )
        return "\n".join(lines) if lines else None


register(SelfCareModule())
