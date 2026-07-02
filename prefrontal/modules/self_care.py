"""Self-care module — the basic-needs check that pierces flow.

Most Prefrontal modules answer *how your ADHD shows up* (time blindness,
hyperfocus, …). This one covers a different, blunter failure mode: in a deep
enough focus state you simply forget to eat. So it's the one cue that
deliberately **overrides the protect-the-flow stance** — a flow state is exactly
when the reminder is needed, not suppressed.

v1 is a single intervention, **meal_check**: starting mid-morning
(``meal_start_hour``, default 11), ask *"have you eaten?"* and keep gently
re-asking every ``meal_reask_minutes`` (default 40) until you confirm — then go
quiet for the rest of the day. It rides the coaching-agent tick
(:meth:`Module.evaluate`), so responsive-hours + debounce come for free (no 2am
food nag), and a one-tap **Ate** / **Snooze** ntfy button (``/nudge/act``)
closes the loop. Off by default — set the ``self_care`` coaching key to ``on``.

Cadence without fighting the engine debounce: the cue's ``dedup_key`` carries a
per-interval *bucket* (which ``reask``-minute window of the day we're in), so
each window fires exactly once — the engine's fire-once-per-``dedup_key`` guard
gives the re-ask rhythm for free, and confirming (``ate_today``) or snoozing
(``meal_snoozed_until``) stops it early.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from prefrontal.coaching import CoachContext, Cue
from prefrontal.modules.base import Intervention, Module, ModuleStore
from prefrontal.modules.registry import register
from prefrontal.scheduling import local_datetime

#: Local clock hour the meal check starts asking (before it, the day's too young).
DEFAULT_MEAL_START_HOUR = 11
#: Minutes between re-asks until you confirm you've eaten.
DEFAULT_MEAL_REASK_MINUTES = 40
#: How long a "Snooze" tap holds the nudge back.
DEFAULT_MEAL_SNOOZE_MINUTES = 30

#: Coaching-state cursors (no schema change — plain key/values).
ATE_TODAY_KEY = "ate_today"            # local "YYYY-MM-DD" once confirmed
SNOOZED_UNTIL_KEY = "meal_snoozed_until"  # UTC "YYYY-MM-DD HH:MM:SS"


def meal_message(name: str = "") -> str:
    """The "have you eaten?" nudge text, greeting by name when we have one."""
    lead = f"{name}, quick check" if name else "Quick check"
    return (
        f"{lead} — have you eaten anything yet today? Even a snack counts. "
        "Tap Ate once you have."
    )


def _parse_ts(ts: Any) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


class SelfCareModule(Module):
    """Nudges the basic needs that a focus state quietly overrides (v1: meals)."""

    key = "self_care"
    title = "Self-Care"
    challenge = (
        "Basic needs — eating, hydrating — dropped during hyperfocus. Not an "
        "executive-function challenge per se but a downstream casualty of one: "
        "the deeper the flow, the easier the meal is to skip."
    )
    default_state = {
        # Master switch — off by default, unlike the EF modules, because a meal
        # nudge is a personal-preference behavior, not a default assist.
        "self_care": "off",
        "meal_start_hour": str(DEFAULT_MEAL_START_HOUR),
        "meal_reask_minutes": str(DEFAULT_MEAL_REASK_MINUTES),
        "meal_snooze_minutes": str(DEFAULT_MEAL_SNOOZE_MINUTES),
    }

    def interventions(self) -> list[Intervention]:
        """Declare the meal-check intervention."""
        return [
            Intervention(
                name="meal_check",
                description=(
                    "From meal_start_hour, ask 'have you eaten?' and re-ask every "
                    "meal_reask_minutes until confirmed — the one cue that fires "
                    "even mid-focus. One-tap Ate / Snooze on ntfy."
                ),
                trigger="mid-morning onward, until you confirm you've eaten",
                status="active",
            ),
        ]

    def evaluate(self, store: ModuleStore, ctx: CoachContext) -> list[Cue]:
        """Fire the meal check when it's due and you haven't confirmed today.

        Opt-in (``self_care`` == ``on``); inert before ``meal_start_hour``, once
        you've confirmed today (``ate_today``), or while snoozed. The engine adds
        responsive-hours + debounce on top, so this never nags overnight.
        """
        if (store.get_state("self_care", "off") or "off") != "on":
            return []

        local = local_datetime(ctx.now, ctx.timezone)
        today = local.strftime("%Y-%m-%d")
        if store.get_state(ATE_TODAY_KEY) == today:
            return []  # already eaten today

        snoozed_until = _parse_ts(store.get_state(SNOOZED_UNTIL_KEY))
        if snoozed_until is not None and ctx.now < snoozed_until:
            return []  # user asked for a bit more time

        start_hour = int(store.get_float("meal_start_hour", DEFAULT_MEAL_START_HOUR))
        reask = int(store.get_float("meal_reask_minutes", DEFAULT_MEAL_REASK_MINUTES))
        reask = max(reask, 1)  # guard a misconfigured 0 from a divide-by-zero
        minute_of_day = local.hour * 60 + local.minute
        start_min = start_hour * 60
        if minute_of_day < start_min:
            return []  # too early in the day to ask

        # Which re-ask window of the day are we in? A fresh bucket each `reask`
        # minutes gives a new dedup_key, so the engine fires it once per window.
        bucket = (minute_of_day - start_min) // reask
        return [
            Cue(
                module=self.key,
                intervention="meal_check",
                urgency="nudge",
                text=meal_message(ctx.display_name),
                context_key="meal",
                dedup_key=f"self_care:meal:{today}:{bucket}",
                # `target` is a synthetic int (the date) so the signed one-tap
                # button has an id to carry; the tap acts on "today", not on it.
                ref={"meal_date": today, "target": int(today.replace("-", ""))},
                suggested_channel="push",
            )
        ]

    def profile_section(self, store: ModuleStore) -> str | None:
        """Report meal-check status when the feature is on."""
        if (store.get_state("self_care", "off") or "off") != "on":
            return None
        start = store.get_state("meal_start_hour") or str(DEFAULT_MEAL_START_HOUR)
        reask = store.get_state("meal_reask_minutes") or str(DEFAULT_MEAL_REASK_MINUTES)
        return (
            f"- Meal check is on: from {start}:00, re-ask every {reask} min until "
            "confirmed — this cue is allowed to interrupt a focus block."
        )


register(SelfCareModule())
