"""Self-care module — the basic-needs checks that a focus state quietly drops.

Most Prefrontal modules answer *how your ADHD shows up* (time blindness,
hyperfocus, …). This one covers a blunter failure mode: in a deep enough focus
state you forget to eat or drink. So these are the cues that deliberately
**override the protect-the-flow stance** — a flow state is exactly when the
reminder is needed, not suppressed.

It's a small **registry of basic-needs checks**, each riding the coaching-agent
tick (:meth:`Module.evaluate`) so responsive-hours + debounce come for free (no
overnight nag). Two ship today, differing only in *mode*:

- **meal** (``once_daily``) — from ``meal_start_hour`` (default 11), ask "have you
  eaten?" and re-ask every ``meal_reask_minutes`` until you confirm, then go quiet
  for the day (``ate_today``). One-tap **Ate** / **Snooze**.
- **water** (``recurring``) — from ``water_start_hour`` (default 9), a "drink some
  water" reminder every ``water_interval_minutes`` (default 90), all day. There's
  no daily "done": a **Drank** tap just pushes the next reminder out a full
  interval; **Snooze** holds it off briefly.

Off by default — set the ``self_care`` coaching key to ``on`` (each check can then
be turned off individually with ``meal_enabled`` / ``water_enabled``).

Cadence without fighting the engine debounce: a cue's ``dedup_key`` carries a
per-interval *bucket* (which window of the day we're in), so each window fires
exactly once — the engine's fire-once-per-``dedup_key`` guard gives the re-ask
rhythm for free. A confirm (``ate_today``) or snooze (``*_snoozed_until``) stops
or defers it; both are plain coaching-state keys, no schema change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from prefrontal.coaching import CoachContext, Cue
from prefrontal.modules.base import Intervention, Module, ModuleStore
from prefrontal.modules.registry import register
from prefrontal.scheduling import local_datetime

#: Meal-check defaults (once-a-day: confirm and it stops for the day).
DEFAULT_MEAL_START_HOUR = 11
DEFAULT_MEAL_REASK_MINUTES = 40
DEFAULT_MEAL_SNOOZE_MINUTES = 30
#: Water-check defaults (recurring: a sip reminder through the day).
DEFAULT_WATER_START_HOUR = 9
DEFAULT_WATER_INTERVAL_MINUTES = 90
DEFAULT_WATER_SNOOZE_MINUTES = 30

#: Meal once-a-day confirm cursor (kept for continuity / external references).
ATE_TODAY_KEY = "ate_today"
#: Meal snooze cursor (UTC "YYYY-MM-DD HH:MM:SS").
SNOOZED_UNTIL_KEY = "meal_snoozed_until"


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


@dataclass(frozen=True)
class BasicCheck:
    """One basic-needs check the module can produce, declared by config keys.

    ``recurring`` is the mode switch: a ``once_daily`` check (meal) has a
    ``done_key`` it stamps to go quiet for the rest of the day; a ``recurring``
    check (water) has none — confirming it just defers the next reminder by a
    full interval. Everything else (start hour, interval, snooze, buttons) is the
    same shape, so a new basic (meds, sleep) is one more entry here plus its
    ntfy buttons.
    """

    key: str                # context_key + ntfy button kind: "meal" | "water"
    intervention: str       # declared Intervention.name
    recurring: bool         # False = once/day (has done_key); True = all-day
    enabled_key: str        # per-check on/off (under the master self_care switch)
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
    confirm_headline: str   # /nudge/act confirmation page copy
    done_key: str | None = None  # once/day terminal cursor (None for recurring)


CHECKS: tuple[BasicCheck, ...] = (
    BasicCheck(
        key="meal",
        intervention="meal_check",
        recurring=False,
        enabled_key="meal_enabled",
        done_key=ATE_TODAY_KEY,
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
        confirm_headline="Nice — glad you ate. 🍽️ I'll leave you be today.",
    ),
    BasicCheck(
        key="water",
        intervention="water_check",
        recurring=True,
        enabled_key="water_enabled",
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
        confirm_headline="Nice — hydrated. 💧 I'll remind you again in a bit.",
    ),
)

#: One-tap action name → the check it resolves (built from CHECKS).
_ACTION_CHECK: dict[str, BasicCheck] = {}
for _c in CHECKS:
    _ACTION_CHECK[_c.confirm_action] = _c
    _ACTION_CHECK[_c.snooze_action] = _c

#: The full set of self-care one-tap actions, for the /nudge/act dispatcher.
SELF_CARE_ACTIONS = frozenset(_ACTION_CHECK)


def _parse_ts(ts: Any) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _stamp(now: datetime, minutes: int) -> str:
    return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def apply_self_care_action(
    store: ModuleStore, action: str, *, now: datetime, today: str
) -> str | None:
    """Apply a one-tap self-care action; return the confirmation copy (or ``None``).

    Centralizes the confirm/snooze semantics so ``/nudge/act`` stays thin:

    - **confirm** on a once-daily check stamps ``done_key`` = today (silent for
      the day); on a recurring check it defers a *full interval* (you just did it).
    - **snooze** defers by the check's snooze minutes.

    Returns ``None`` for an unknown action (so the caller can fall through).
    """
    check = _ACTION_CHECK.get(action)
    if check is None:
        return None
    if action == check.confirm_action:
        if check.recurring:
            interval = max(1, int(store.get_float(check.interval_key, check.interval_default)))
            store.set_state(check.snooze_key, _stamp(now, interval), source="explicit")
        else:
            store.set_state(check.done_key or ATE_TODAY_KEY, today, source="explicit")
        return check.confirm_headline
    mins = int(store.get_float(check.snooze_minutes_key, check.snooze_minutes_default))
    store.set_state(check.snooze_key, _stamp(now, mins), source="explicit")
    return f"Okay — I'll check back in {mins} min."


class SelfCareModule(Module):
    """Nudges the basic needs a focus state quietly overrides (meals, water)."""

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
        "water_enabled": "on",
        "water_start_hour": str(DEFAULT_WATER_START_HOUR),
        "water_interval_minutes": str(DEFAULT_WATER_INTERVAL_MINUTES),
        "water_snooze_minutes": str(DEFAULT_WATER_SNOOZE_MINUTES),
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
                    "water_interval_minutes through the day. One-tap Drank (defers a "
                    "full interval) / Snooze on ntfy."
                ),
                trigger="through the day, on an interval",
                status="active",
            ),
        ]

    def evaluate(self, store: ModuleStore, ctx: CoachContext) -> list[Cue]:
        """Fire whichever basic-needs checks are due right now.

        Opt-in (``self_care`` == ``on``); each check inert before its start hour,
        while snoozed, or (once-daily) once confirmed today. The engine adds
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
            if check.done_key and store.get_state(check.done_key) == today:
                continue  # once-daily and already confirmed today
            snoozed_until = _parse_ts(store.get_state(check.snooze_key))
            if snoozed_until is not None and ctx.now < snoozed_until:
                continue  # deferred (snooze, or a recurring "just did it")

            start_hour = int(store.get_float(check.start_hour_key, check.start_hour_default))
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

    def profile_section(self, store: ModuleStore) -> str | None:
        """Report which basic-needs checks are on."""
        if (store.get_state("self_care", "off") or "off") != "on":
            return None
        lines: list[str] = []
        for check in CHECKS:
            if (store.get_state(check.enabled_key, "on") or "on") == "off":
                continue
            start = store.get_state(check.start_hour_key) or str(check.start_hour_default)
            every = store.get_state(check.interval_key) or str(check.interval_default)
            kind = "re-ask" if not check.recurring else "repeat"
            lines.append(
                f"- {check.key.title()} check on: from {start}:00, {kind} every "
                f"{every} min — allowed to interrupt a focus block."
            )
        return "\n".join(lines) if lines else None


register(SelfCareModule())
