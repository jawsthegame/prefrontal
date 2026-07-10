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
from prefrontal.coaching import CoachContext, Cue, _fired_key, note_hint
from prefrontal.delegation import checkin_interval_hours, checkin_message
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register


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
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit at most one check-in — the most-overdue parked delegation.

        Self-gated: for each parked todo, compare the time since its check-in last
        fired (the engine's ``coach_fired:`` stamp) against its item-dependent
        interval, and only surface the one that is furthest past due. The engine
        still applies quiet hours and its own debounce downstream.
        """
        best: tuple[float, Cue] | None = None
        for todo in store.actively_delegated_todos():
            delegation = todo.get("delegation")
            if not delegation:
                continue
            text = checkin_message(todo, delegation, ctx.now)
            if not text:
                continue  # in_prep etc. — nothing worth a nudge yet
            interval = checkin_interval_hours(todo, delegation, ctx.now)
            dedup_key = f"delegation_checkin:{todo['id']}"
            last = _parse_ts(store.get_state(_fired_key(dedup_key)))
            elapsed = (
                (ctx.now - last).total_seconds() / 3600.0 if last is not None else None
            )
            if elapsed is not None and elapsed < interval:
                continue  # checked in recently enough
            # How far past due (first-ever check-in counts as maximally due).
            overdue = 999.0 if elapsed is None else elapsed / interval
            cue = Cue(
                module=self.key,
                intervention="delegation_checkin",
                urgency="nudge",
                text=text + note_hint(todo.get("notes")),
                context_key="todo",
                dedup_key=dedup_key,
                ref={"todo_id": todo["id"], "delegation_status": delegation.get("status")},
            )
            if best is None or overdue > best[0]:
                best = (overdue, cue)
        return [best[1]] if best is not None else []

    def profile_section(self, store: MemoryStore) -> str | None:
        """Note how many hand-offs are currently in flight (light context)."""
        parked = store.actively_delegated_todos()
        if not parked:
            return None
        waiting = sum(1 for t in parked if (t.get("delegation") or {}).get("status") == "forwarded")
        bits = [f"{len(parked)} delegated todo(s) currently parked"]
        if waiting:
            bits.append(f"{waiting} awaiting a human assistant")
        return "- " + "; ".join(bits) + "."


register(DelegationCheckinModule())
