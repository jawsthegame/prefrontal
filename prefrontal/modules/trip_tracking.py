"""Closed-Loop Trip Tracking module.

The passive companion to the Location-Aware Task Anchor. Where the anchor nudges
you back from a *declared* outing, this module notices the trips you *didn't*
declare: leave the home radius, come back to it, and the round trip is logged as a
closed loop (see :mod:`prefrontal.trips`). Because nothing was stated up front,
this module's job is to **ask, after the fact**, for a label and a category — and
to invite an honest, plain-English note on how it went, which is classified into
an outcome that feeds the learning loop.

The detection itself runs in the location-ingestion path; this module owns the
coaching-agent surface (the gentle "what was that trip?" ask) and the behavioral-
profile section. The ask is deliberately **ambient** — it rides the briefing/digest
rather than interrupting, since a trip you've already finished is never urgent.
"""

from __future__ import annotations

from prefrontal.coaching import CoachContext, Cue
from prefrontal.focus_balance import (
    build_focus_balance,
    format_minutes,
    nudge_enabled,
    underserved_nudge_text,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import minutes_between

#: Cap how many unlabeled trips we ask about per tick, so a stretch of untracked
#: days can't turn into a wall of asks. Newest first — the freshest trip is the
#: one you can still remember.
MAX_LABEL_ASKS = 3


class ClosedLoopTripModule(Module):
    """Tracks auto-detected round trips and asks the user to label + review them."""

    key = "trip_tracking"
    title = "Closed-Loop Trip Tracking"
    challenge = (
        "Trips taken without declaring them — you leave, you come back, and the "
        "time and context are gone. Passive tracking captures the round trip; a "
        "quick label, category, and honest 'how it went' turns it into signal."
    )

    def interventions(self) -> list[Intervention]:
        """Declare the label ask and the plain-English reflection capture."""
        return [
            Intervention(
                name="label_prompt",
                description="Ask for a label + category on a completed, undeclared trip.",
                trigger="a location loop closes (left the home radius, then returned)",
                status="active",
            ),
            Intervention(
                name="reflection_capture",
                description=(
                    "Accept a plain-English 'how it went' note, classify it into an "
                    "outcome, and feed it back into stats + the learning engine."
                ),
                trigger="the user reflects on a completed trip",
                status="active",
            ),
            Intervention(
                name="focus_balance",
                description=(
                    "A gentle weekly heads-up when out-of-home time skews away from a "
                    "life-sphere you've set a target for (kids/personal) — well-being, "
                    "not productivity. Opt-in via the `focus_balance_nudge` state flag."
                ),
                trigger="a targeted life-domain falls under half its weekly aim",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit an ambient ask for each recently-completed, still-unlabeled trip.

        Ambient urgency: it's folded into the digest/briefing and never interrupts
        — the trip is already over. One cue per trip (``dedup_key`` keys on the
        trip id), and a labeled trip drops out of :meth:`unlabeled_trips`, so each
        trip is asked about at most once.
        """
        # Imported lazily: prefrontal.trips depends on this module's package, so a
        # top-level import here would be a cycle at package-init time.
        from prefrontal.trips import trip_label_prompt

        cues: list[Cue] = []
        for trip in store.unlabeled_trips(limit=MAX_LABEL_ASKS):
            # recent/unlabeled rows don't carry a computed actual_minutes; derive
            # it from the stored timestamps so the ask can mention the duration.
            enriched = dict(trip)
            enriched.setdefault(
                "actual_minutes",
                minutes_between(trip.get("departed_at"), trip.get("returned_at")),
            )
            cues.append(
                Cue(
                    module=self.key,
                    intervention="label_prompt",
                    urgency="ambient",
                    text=trip_label_prompt(enriched),
                    context_key="trip",
                    dedup_key=f"trip_label:{trip['id']}",
                    ref={"trip_id": trip["id"]},
                )
            )

        cues.extend(self._focus_balance_cues(store, ctx))
        return cues

    def _focus_balance_cues(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """At most one weekly "you're light on <sphere>" nudge, when opted in.

        Inert unless the ``focus_balance_nudge`` flag is set *and* a targeted
        life-domain is running under half its weekly aim — the common case is
        silence. Ambient (rides the digest, never interrupts) and deduped to the
        ISO week so it can't fire more than once a week however often the tick runs.
        """
        if not nudge_enabled(store):
            return []
        balance = build_focus_balance(store, now=ctx.now)
        text = underserved_nudge_text(balance)
        if text is None:
            return []
        iso = ctx.now.isocalendar()
        return [
            Cue(
                module=self.key,
                intervention="focus_balance",
                urgency="ambient",
                text=text,
                context_key="focus_balance",
                dedup_key=f"focus_balance:{iso[0]}-W{iso[1]:02d}",
                ref={"days": balance.days},
            )
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize recent closed-loop trips: volume, categories, and how they went."""
        trips = [t for t in store.recent_trips(limit=100) if t.get("status") == "completed"]
        if not trips:
            return None

        durations = [
            m
            for t in trips
            if (m := minutes_between(t.get("departed_at"), t.get("returned_at"))) is not None
        ]
        lines = [f"Closed-loop trips tracked recently: {len(trips)}."]
        if durations:
            avg = sum(durations) / len(durations)
            lines.append(f"Average time out: {round(avg)} min.")

        cats: dict[str, int] = {}
        for t in trips:
            if t.get("category"):
                cats[t["category"]] = cats.get(t["category"], 0) + 1
        if cats:
            top = sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))
            lines.append(
                "By category: " + ", ".join(f"{name} ×{n}" for name, n in top) + "."
            )

        # Focus balance: where the last week's out-of-home time went, by life-sphere
        # (shop/work/home/kids/personal) — the axis the coaching note weighs "am I
        # spreading my focus?" against. Silent when no completed trips land in-window.
        balance = build_focus_balance(store)
        if balance.has_data:
            by_domain = ", ".join(
                f"{d.domain} {format_minutes(d.minutes)}"
                for d in balance.domains
                if d.minutes > 0
            )
            lines.append(f"Focus balance (last {balance.days}d out): {by_domain}.")
            for gap in balance.underserved():
                lines.append(
                    f"Light on {gap.domain}: {format_minutes(gap.minutes)} vs a "
                    f"{format_minutes(gap.target_minutes or 0)} weekly aim."
                )

        reflected = [t for t in trips if t.get("reflection_outcome")]
        if reflected:
            rough = sum(1 for t in reflected if t["reflection_outcome"] == "miss")
            lines.append(
                f"Self-reported: {len(reflected)} of {len(trips)} reflected on, "
                f"{rough} went rough (their honest read)."
            )

        unlabeled = sum(1 for t in trips if not t.get("label"))
        if unlabeled:
            lines.append(f"{unlabeled} still unlabeled — worth naming while fresh.")
        return "\n".join(f"- {line}" for line in lines)


register(ClosedLoopTripModule())
