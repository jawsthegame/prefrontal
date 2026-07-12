"""Delegation follow-through module.

Delegating a todo is meant to get it *off your plate* — so a parked hand-off is
pulled out of the active do-it-now surfaces (``open_todos(exclude_delegated=True)``)
and should only resurface on a **slower, item-dependent cadence**: "heard back from
your assistant yet?" for a human hand-off, "your prep is ready" for an agent one.

This module owns that re-surfacing. It self-gates on
:func:`prefrontal.delegation.checkin_interval_hours` (near a deadline → daily; no
deadline / low priority → ~weekly; an agent brief sitting ready → soon) by reading
the engine's own last-fired stamp, so it can pace itself far slower than the
coaching engine's uniform debounce. At most one check-in per tick (the most-overdue
one), so several parked hand-offs trickle out rather than arriving in a burst.
"""

from __future__ import annotations

from typing import Any

from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.coaching import (
    CoachContext,
    Cue,
    last_fired,
    note_hint,
    stamp_pending_engagement,
    sweep_engagement,
)
from prefrontal.delegation import (
    DEFAULT_STALLED_CHECKIN_MISSES,
    STATUS_FORWARDED,
    checkin_interval_hours,
    checkin_message,
    stalled_handoff_message,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: episode_type for a check-in nudge's engagement outcome (see :meth:`before_collect`).
ENGAGE_EPISODE = "delegation"


class DelegationCheckinModule(Module):
    """Re-surface a parked delegation on a slow, item-dependent check-in cadence."""

    key = "delegation_checkin"
    title = "Delegation follow-through"
    challenge = (
        "Delegating something only helps if it truly leaves your plate but still "
        "closes the loop — otherwise it either keeps nagging like undone work or "
        "silently falls through the cracks."
    )

    def interventions(self) -> list[Intervention]:
        return [
            Intervention(
                name="delegation_checkin",
                description=(
                    "Gently re-surface a delegated todo on a cadence set by the item "
                    "(deadline/priority/handler) — 'heard back?' for a human hand-off, "
                    "'your prep is ready' for an agent one."
                ),
                trigger="a parked delegation has gone quiet past its check-in interval",
                status="active",
            ),
            Intervention(
                name="stalled_escalation",
                description=(
                    "After repeated ignored check-ins on a forwarded hand-off with no "
                    "movement, escalate the copy from 'heard back?' to a decision "
                    "prompt — take it back, re-delegate, or drop it — so a dead "
                    "hand-off gets resolved instead of rotting on the parked list."
                ),
                trigger="a forwarded hand-off has drawn delegation_stall_misses ignored check-ins",
                status="active",
            ),
        ]

    def _ignored_streak(self, store: MemoryStore, todo_id: int) -> int:
        """Consecutive ignored check-ins for ``todo_id`` since its last movement.

        Counts recent ``delegation`` engagement episodes (from
        :meth:`before_collect`) for this todo, newest first, stopping at the first
        ``success`` — a check-in that *did* move the hand-off resets the streak.
        """
        target_ctx = f"{self.key} nudge: {todo_id}"
        streak = 0
        for ep in store.episodes_by_type(ENGAGE_EPISODE, limit=100):
            if (ep.get("context") or "") != target_ctx:
                continue
            if ep.get("outcome") == "success":
                break  # it moved after that check-in — streak resets
            if ep.get("outcome") == "ignored":
                streak += 1
        return streak

    def _stall_threshold(self, store: MemoryStore) -> int:
        """The ignored-check-in count that trips escalation (default, on a bad value too)."""
        try:
            return int(
                store.get_state("delegation_stall_misses") or DEFAULT_STALLED_CHECKIN_MISSES
            )
        except (TypeError, ValueError):
            return DEFAULT_STALLED_CHECKIN_MISSES

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit at most one check-in — the most-overdue parked delegation.

        Self-gated: for each parked todo, compare the time since its check-in last
        fired (the engine's ``coach_fired:`` stamp) against its item-dependent
        interval, and only surface the one that is furthest past due. The engine
        still applies quiet hours and its own debounce downstream.
        """
        threshold = self._stall_threshold(store)
        best: tuple[float, Cue] | None = None
        for todo in store.actively_delegated_todos():
            delegation = todo.get("delegation")
            if not delegation:
                continue
            # A forwarded hand-off that keeps drawing ignored check-ins escalates
            # from "heard back?" to a take-back/re-delegate/drop decision prompt.
            misses = (
                self._ignored_streak(store, todo["id"])
                if delegation.get("status") == STATUS_FORWARDED
                else 0
            )
            stalled = misses >= threshold
            if stalled:
                text = stalled_handoff_message(todo, delegation, misses, ctx.now)
                intervention = "stalled_escalation"
            else:
                text = checkin_message(todo, delegation, ctx.now)
                intervention = "delegation_checkin"
            if not text:
                continue  # in_prep etc. — nothing worth a nudge yet
            interval = checkin_interval_hours(todo, delegation, ctx.now)
            dedup_key = f"delegation_checkin:{todo['id']}"
            last = last_fired(store, dedup_key)
            elapsed = (
                (ctx.now - last).total_seconds() / 3600.0 if last is not None else None
            )
            if elapsed is not None and elapsed < interval:
                continue  # checked in recently enough
            # How far past due (first-ever check-in counts as maximally due); a
            # stalled hand-off sorts ahead of ordinary check-ins so the decision
            # prompt wins the one-per-tick slot.
            overdue = (999.0 if elapsed is None else elapsed / interval) + (
                1000.0 if stalled else 0.0
            )
            cue = Cue(
                module=self.key,
                intervention=intervention,
                urgency="nudge",
                text=text + note_hint(todo.get("notes")),
                context_key="todo",
                dedup_key=dedup_key,
                ref={
                    "todo_id": todo["id"],
                    "delegation_status": delegation.get("status"),
                    "stalled": stalled,
                },
            )
            if best is None or overdue > best[0]:
                best = (overdue, cue)
        return [best[1]] if best is not None else []

    def before_collect(self, store: MemoryStore, ctx: CoachContext) -> None:
        """Mature prior check-ins into engagement outcomes.

        A check-in has no tappable answer; its honest signal is whether the
        delegation *moved* afterward — the todo advanced (its status stepped
        forward or a reply matched), closed, or left the parked set entirely. Once
        the item's own check-in interval has passed with no such movement, log an
        ``ignored`` outcome; movement logs ``success``. Runs via the engine's
        ``before_collect`` hook — the learning signal a bare ``evaluate`` never fed
        back.
        """
        parked = {t["id"]: t for t in store.actively_delegated_todos()}

        def verdict(target: str, fired: Any) -> str | None:
            todo = parked.get(int(target))
            if todo is None:
                return "engaged"  # closed or advanced out of the parked-delegated set
            delegation = todo.get("delegation") or {}
            interval = checkin_interval_hours(todo, delegation, ctx.now)
            if (ctx.now - fired).total_seconds() / 3600.0 < interval:
                return None  # give this item its own check-in cadence before judging
            updated = _parse_ts(todo.get("updated_at"))
            return "engaged" if (updated is not None and updated > fired) else "ignored"

        sweep_engagement(
            store, ctx.now, module=self.key, episode_type=ENGAGE_EPISODE, verdict=verdict
        )

    def after_fire(self, store: MemoryStore, decisions: list[Any], ctx: CoachContext) -> None:
        """Mark each fired check-in as awaiting an engagement verdict."""
        stamp_pending_engagement(
            store, decisions, ctx.now, module=self.key, target_key="todo_id"
        )

    def profile_section(self, store: MemoryStore) -> str | None:
        """Note how many hand-offs are currently in flight (light context)."""
        parked = store.actively_delegated_todos()
        if not parked:
            return None
        waiting = sum(1 for t in parked if (t.get("delegation") or {}).get("status") == "forwarded")
        bits = [f"{len(parked)} delegated todo(s) currently parked"]
        if waiting:
            bits.append(f"{waiting} awaiting a human assistant")
        lines = ["- " + "; ".join(bits) + "."]
        # Fold in how check-ins have landed (from before_collect's outcomes) so the
        # profile reflects whether re-surfacing a parked hand-off actually moves it.
        outcomes = store.episodes_by_type(ENGAGE_EPISODE, limit=50)
        if outcomes:
            engaged = sum(1 for e in outcomes if e.get("outcome") == "success")
            lines.append(
                f"- Delegation check-ins land {engaged}/{len(outcomes)} of the time "
                "(hand-off moved afterward)."
            )
        return "\n".join(lines)


register(DelegationCheckinModule())
