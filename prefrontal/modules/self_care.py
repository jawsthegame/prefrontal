"""Self-care module — the basic-needs checks that a focus state quietly drops.

Most Prefrontal modules answer *how your ADHD shows up* (time blindness,
hyperfocus, …). This one covers a blunter failure mode: in a deep enough focus
state you forget to eat or drink. So these are the cues that deliberately
**override the protect-the-flow stance** — a flow state is exactly when the
reminder is needed, not suppressed.

It's a small **registry of basic-needs checks**, each riding the coaching-agent
tick (:meth:`Module.evaluate`) so responsive-hours + debounce come for free (no
overnight nag). Two ship today, differing only in their **daily target**:

- **meal** (target 1) — from ``meal_start_hour`` (default 11), ask "have you
  eaten?" and re-ask every ``meal_reask_minutes`` until you confirm; one "Ate"
  meets the target and it goes quiet for the day.
- **water** (target ``water_daily_target``, default 6) — from ``water_start_hour``
  (default 9), a "drink some water" reminder every ``water_interval_minutes``
  (default 90); each **Drank** counts one toward the target and pushes the next
  reminder out a full interval, and once you hit the target it's done for the day.

So "how many yeses stops it for the day" is just the target: 1 for a meal, N for
water. Off by default — set the ``self_care`` coaching key to ``on`` (each check
can then be turned off individually with ``meal_enabled`` / ``water_enabled``).

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
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import TS_FMT
from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.coaching import CoachContext, Cue
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import local_datetime

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


@dataclass(frozen=True)
class BasicCheck:
    """One basic-needs check, declared by its config keys and daily target.

    The **target** is the unifying knob: a once-a-day check (meal) is just target
    1; a recurring one (water) is target N. A confirm counts one toward the day's
    target and — until the target is met — defers the next reminder a full
    interval; meeting the target ends the check for the day. Adding a new basic
    (meds, sleep) is one more entry here plus its ntfy buttons.
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
) -> bool:
    """Whether a check is "behind" right now — the flag a surface flashes on (pure).

    Never overdue when disabled, already met, or before the start hour. A once-a-day
    check (``target <= 1``) is overdue simply once past its start hour and not done.
    A recurring check is *pace-aware*: since its start hour you'd expect about one
    confirm per ``interval`` minutes, so it's overdue only when ``count`` has fallen
    below that running expectation — so water flags when you've genuinely lagged the
    pace, not merely because you're not yet at the daily total. Expectation is capped
    at ``target`` (you're never "behind" once you've done enough).
    """
    if not enabled or done or local.hour < start_hour:
        return False
    if target <= 1:
        return True
    minutes_since = (local.hour - start_hour) * 60 + local.minute
    expected = min(target, minutes_since // interval)
    return count < expected


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
    checks: list[dict[str, Any]] = []
    for check in CHECKS:
        count = day_count(store, check, today)
        target = _target(store, check)
        enabled = (store.get_state(check.enabled_key, "on") or "on") == "on"
        done = count >= target
        start_hour = store.get_hour(check.start_hour_key, check.start_hour_default)
        interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
        checks.append(
            {
                "key": check.key,
                "enabled": enabled,
                "count": count,
                "target": target,
                "done": done,
                "start_hour": start_hour,
                # "You're behind on this" — the flag a surface flashes on. Always
                # false when done, disabled, or before the start hour. For a
                # once-a-day check (target 1) that's simply "past the start hour and
                # still not done". For a recurring check (water) it's *pace-aware*:
                # you'd expect roughly one per interval since the start hour, so it
                # flags only when the count has fallen short of that running
                # expectation — it clears as you catch up, instead of nagging all
                # day just because you're not yet at the daily total.
                "overdue": _is_overdue(
                    enabled=enabled, done=done, target=target, count=count,
                    start_hour=start_hour, interval=interval, local=local,
                ),
                "interval_minutes": interval,
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
        checks: ``{check_key: {enabled?, target?, start_hour?, interval_minutes?}}``.
            Unknown check keys are ignored; absent fields are left untouched.
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
        if cfg.get("target") is not None:
            store.set_state(check.target_key, str(max(1, int(cfg["target"]))), source="explicit")
        if cfg.get("start_hour") is not None:
            hour = str(min(23, max(0, int(cfg["start_hour"]))))
            store.set_state(check.start_hour_key, hour, source="explicit")
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
        store.log_episode(
            SELF_CARE_EPISODE,
            acknowledged=True,
            context=f"{check.key}: confirmed",
            outcome="confirmed",
            notes=f"{count}/{target} {lat}",
        )
        if count >= target:
            return check.done_headline.format(count=count, target=target)
        # Not there yet — space the next reminder a full interval out.
        interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
        store.set_state(check.snooze_key, _stamp(now, interval), source="explicit")
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
    return f"Removed one — {count}/{_target(store, check)} today."


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
    """Nudges the basic needs a focus state quietly overrides (meals, water, meds)."""

    key = "self_care"
    title = "Self-Care"
    challenge = (
        "Basic needs — eating, hydrating — dropped during hyperfocus. Not an "
        "executive-function challenge per se but a downstream casualty of one: "
        "the deeper the flow, the easier a meal or a glass of water is to skip."
    )
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
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Fire whichever basic-needs checks are due right now.

        Opt-in (``self_care`` == ``on``); each check inert before its start hour,
        while snoozed, or once the day's target is met. The engine adds
        responsive-hours + debounce on top, so nothing nags overnight.
        """
        if (store.get_state("self_care", "off") or "off") != "on":
            return []

        local = local_datetime(ctx.now, ctx.timezone)
        today = local.strftime("%Y-%m-%d")
        minute_of_day = local.hour * 60 + local.minute

        cues: list[Cue] = []
        for check in CHECKS:
            if (store.get_state(check.enabled_key, "on") or "on") == "off":
                continue
            if day_count(store, check, today) >= _target(store, check):
                continue  # hit the day's target already
            snoozed_until = _parse_ts(store.get_state(check.snooze_key))
            if snoozed_until is not None and ctx.now < snoozed_until:
                continue  # deferred (snooze, or the interval after a confirm)

            start_hour = store.get_hour(check.start_hour_key, check.start_hour_default)
            interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
            start_min = start_hour * 60
            if minute_of_day < start_min:
                continue  # too early in the day for this check

            # A fresh bucket each `interval` minutes → a new dedup_key, so the
            # engine fires each window once (the re-ask/repeat rhythm).
            bucket = (minute_of_day - start_min) // interval
            cues.append(
                Cue(
                    module=self.key,
                    intervention=check.intervention,
                    urgency="nudge",
                    text=check.message(ctx.display_name),
                    context_key=check.key,
                    dedup_key=f"self_care:{check.key}:{today}:{bucket}",
                    # `target` is a synthetic int (the date) so the signed one-tap
                    # button has an id to carry; the tap acts on "now", not on it.
                    ref={"date": today, "target": int(today.replace("-", ""))},
                    suggested_channel="push",
                )
            )
        return cues

    def profile_section(self, store: MemoryStore) -> str | None:
        """Report which basic-needs checks are on."""
        if (store.get_state("self_care", "off") or "off") != "on":
            return None
        lines: list[str] = []
        for check in CHECKS:
            if (store.get_state(check.enabled_key, "on") or "on") == "off":
                continue
            start = store.get_hour(check.start_hour_key, check.start_hour_default)
            every = store.get_state(check.interval_key) or str(check.interval_default)
            target = _target(store, check)
            goal = "" if target == 1 else f" (up to {target}/day)"
            lines.append(
                f"- {check.key.title()} check on: from {start}:00, every {every} min"
                f"{goal} — allowed to interrupt a focus block."
            )
        return "\n".join(lines) if lines else None


register(SelfCareModule())
