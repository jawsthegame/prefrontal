"""Implementation Intentions ("if-then plans").

Implementation intentions are the single most strongly-evidenced self-regulation
technique for ADHD: pre-committing a concrete *cue* to a specific *action* —
"**if** I sit down at my desk after lunch, **then** I open the tax form and set a
5-minute timer" — reliably closes the intention-action gap (Gollwitzer & Sheeran
2006, d≈0.65; Gawrilow & Gollwitzer 2008 restored inhibitory control in children
with ADHD toward non-ADHD levels). The mechanism is *delegation of initiation to
the environment*: the cue, not effortful executive function, triggers the action.

So the payoff is entirely about **when** the plan is surfaced. A plan sitting in a
list is just another todo; a plan re-shown *at the moment its cue is detected* is
the intervention. This module is therefore a thin cue-matcher over the coaching
tick — it owns no delivery, no clock, no escalation. Each tick the engine hands it
:class:`~prefrontal.coaching.CoachContext` (``now`` + current location); for every
active plan whose cue is satisfied *right now* it returns one gentle
:class:`~prefrontal.coaching.Cue` re-stating the pre-decided action. The engine's
debounce keeps it from nagging, quiet hours gate it, and the shared channel choice
delivers it — all for free.

The cue is an **AND** over whatever constraints a plan sets (at least one required):

- ``cue_place`` — a curated :class:`place <prefrontal.geo>` matched by proximity
  (reusing :func:`prefrontal.geo.nearest_place`, the same matcher trips use), so
  "at my desk" fires only when the phone is actually there.
- ``cue_window`` — a local time-of-day band ``"HH:MM-HH:MM"`` (parsed by
  :func:`prefrontal.scheduling.parse_window`, midnight-wrap aware), so "after lunch"
  is ``12:30-14:00``.
- ``cue_event`` — a detected *transition* (``arrive_home`` / ``leave_home``),
  edge-detected each tick against the last-known home presence (the same home
  coordinate + radius the trip tracker uses). Event/cue-based prospective memory is
  the strongest channel — stronger than clock-based — so "**when I get home**, take
  out the recycling" fires once *at* the crossing, not on a timer.

"At my desk after lunch" is place+window; "when I get home in the evening" is
event+window; each fires only when all its set constraints hold. This composes the
existing location, time, and home-presence primitives rather than adding a new
geofence engine.

**Forgiving by design.** There is no streak, no "you missed it," no guilt. A plan
you stop using is simply *archived* (a neutral retire), never a broken chain — the
technique works by lowering the cost of starting, and shame raises it. Storage is
the :class:`~prefrontal.memory.repos.plans.PlansRepo` (``implementation_intentions``
table); capture is one utterance through the NL assistant (``add_if_then``).
"""

from __future__ import annotations

from datetime import datetime

from prefrontal.clock import TS_FMT, local_datetime
from prefrontal.coaching import CoachContext, Cue, Decision
from prefrontal.geo import (
    DEFAULT_HOME_RADIUS_M,
    DEFAULT_PLACE_MATCH_RADIUS_M,
    haversine_m,
    nearest_place,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import parse_window

#: The transition events a plan can cue on, detected each tick by edge detection
#: against the last-known home presence. Event/cue-based prospective memory is the
#: strongest channel (stronger than clock-based), and "when I get home / when I
#: leave" is its canonical, high-frequency case — reusing the home coordinate and
#: radius the trip tracker already relies on, no new geofence engine.
CUE_EVENTS: tuple[str, ...] = ("arrive_home", "leave_home")

#: Coaching-state key holding the last-observed home presence (``"home"``/``"away"``)
#: so a *transition* can be detected across ticks. Advanced in :meth:`after_fire`
#: (a write) from the same presence :meth:`evaluate` read, mirroring how the
#: location anchor keeps ``evaluate`` a pure read and applies state in ``after_fire``.
HOME_PRESENCE_STATE_KEY = "if_then_home_presence"


def _in_span(minute: int, span: tuple[int, int]) -> bool:
    """Whether ``minute`` (0–1439) is inside ``[start, end)``, midnight-wrap aware.

    A local mirror of the same check :mod:`prefrontal.scheduling` uses for its
    suggestion windows (kept module-private there); three lines not worth a shared
    import, and it keeps this module's cue logic self-contained and pure.
    """
    start, end = span
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps past midnight


def within_window(local_dt: datetime, window: str | None) -> bool:
    """Whether a local wall-clock time falls inside a plan's ``"HH:MM-HH:MM"`` band.

    ``False`` for a missing or unparseable window, so a time cue that can't be read
    simply never fires (rather than firing always) — the same fail-closed stance the
    todo suggestion windows take.
    """
    parsed = parse_window(window)
    if parsed is None:
        return False
    return _in_span(local_dt.hour * 60 + local_dt.minute, parsed)


def home_presence(
    ctx: CoachContext, home: dict | None, *, radius_m: float
) -> str | None:
    """Whether the user is currently ``"home"`` / ``"away"``, or ``None`` if unknown.

    ``None`` when either the phone's location or the stored home coordinate is
    missing — an unknown presence must never be treated as a transition. Uses the
    same great-circle distance + home radius the trip tracker and location anchor
    use, so "home" means the same footprint everywhere.
    """
    if ctx.current_lat is None or ctx.current_lon is None or not home:
        return None
    distance = haversine_m(home["lat"], home["lon"], ctx.current_lat, ctx.current_lon)
    return "home" if distance <= radius_m else "away"


def detect_events(prior: str | None, current: str | None) -> frozenset[str]:
    """The transition events between the last-known and current home presence (pure).

    Edge detection, not level: ``arrive_home`` fires only on an ``away``→``home``
    flip, ``leave_home`` only on ``home``→``away``. Returns nothing when presence is
    unknown (``current is None``) or there's no baseline yet (``prior is None``) or
    presence is unchanged — so a plan cued on an event fires once *at* the crossing,
    not every tick the user then sits at home.
    """
    if current is None or prior is None or prior == current:
        return frozenset()
    if current == "home":
        return frozenset({"arrive_home"})
    return frozenset({"leave_home"})


def plan_cue_active(
    plan: dict,
    *,
    current_place_name: str | None,
    local_dt: datetime | None,
    fired_events: frozenset[str] = frozenset(),
) -> bool:
    """Whether an if-then plan's cue is satisfied right now (pure).

    The cue is an AND over the constraints the plan actually sets: a ``cue_place``
    must match the place the user is currently at (by normalized name), a
    ``cue_window`` must contain the current local time, and a ``cue_event`` must be
    among the transitions detected *this tick*. A plan with **no** cue set never
    fires — there is nothing to trigger it — so a half-captured plan stays quiet
    rather than firing on every tick.

    Args:
        plan: A plan row (``cue_place``/``cue_window``/``cue_event``).
        current_place_name: The normalized ``places.name`` the user is currently
            within, or ``None`` when location is unknown / not at a curated place.
        local_dt: The current local wall-clock datetime, or ``None`` when unknown.
        fired_events: Transition events detected this tick (see :func:`detect_events`).

    Returns:
        ``True`` only when at least one cue constraint is set and every set
        constraint is currently satisfied.
    """
    has_constraint = False
    place = (plan.get("cue_place") or "").strip()
    if place:
        has_constraint = True
        if current_place_name is None or current_place_name != place:
            return False
    window = (plan.get("cue_window") or "").strip()
    if window:
        has_constraint = True
        if local_dt is None or not within_window(local_dt, window):
            return False
    event = (plan.get("cue_event") or "").strip()
    if event:
        has_constraint = True
        if event not in fired_events:
            return False
    return has_constraint


def build_plan_message(plan: dict) -> str:
    """The gentle, cue-first nudge re-stating a plan's pre-decided action.

    Leads with the cue (the trigger the user is *at* right now) and hands back the
    pre-committed step verbatim, framed as "the whole task is just this" — never as
    a chore owed or a streak at stake.
    """
    cue = (plan.get("cue_text") or "your cue").strip()
    action = (plan.get("action_text") or "").strip()
    return f"You're at your cue — {cue}. Your pre-set move: {action}"


class ImplementationIntentionModule(Module):
    """Surfaces a pre-committed if-then plan the moment its cue is detected."""

    key = "implementation_intention"
    title = "Implementation Intentions"
    challenge = (
        "The gap between intending to start something and actually starting it. An "
        "if-then plan pre-decides one concrete action for a concrete cue, so the cue "
        "— not effortful executive function — triggers the start."
    )

    def channel_targets(self) -> dict[str, str]:
        """If-then nudges carry a one-tap action button, keyed by ``plan_id``.

        Makes the nudge's outcome observable (a tap → a channel-response *success*,
        silence → a *miss*), so which channel actually lands an if-then reminder is
        learned like every other module's — see
        :func:`prefrontal.coaching.channel_targets_for`.
        """
        return {"if_then": "plan_id"}

    def interventions(self) -> list[Intervention]:
        """Declare the one intervention: surface the plan at its cue."""
        return [
            Intervention(
                name="surface_at_cue",
                description=(
                    "Re-show a pre-committed if-then plan's tiny action the moment "
                    "its cue is detected (arriving at a place, entering a time-of-day "
                    "band, crossing home — arrive/leave — or a combination) — "
                    "offloading task initiation onto the cue."
                ),
                trigger="the coaching tick detects a plan's cue is currently satisfied",
                status="active",
            ),
        ]

    def _current_place_name(self, store: MemoryStore, ctx: CoachContext) -> str | None:
        """The normalized name of the curated place the user is currently within, or ``None``.

        Reuses the trips/anchor proximity matcher (:func:`prefrontal.geo.nearest_place`)
        and the same tunable ``place_match_radius_m`` coaching key, so an if-then place
        cue fires on exactly the footprint a trip stop snaps to. ``None`` whenever
        location is unavailable or the user isn't near any curated place.
        """
        if ctx.current_lat is None or ctx.current_lon is None:
            return None
        radius = store.get_float("place_match_radius_m", DEFAULT_PLACE_MATCH_RADIUS_M)
        match = nearest_place(
            store.places(), ctx.current_lat, ctx.current_lon, radius_m=radius
        )
        return match[0].get("name") if match is not None else None

    def _current_home_presence(self, store: MemoryStore, ctx: CoachContext) -> str | None:
        """The user's current home presence (``"home"``/``"away"``/``None``) this tick."""
        radius = store.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M)
        return home_presence(ctx, store.get_home(), radius_m=radius)

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit a nudge for each active plan whose cue is satisfied right now.

        Read-only (honoring the :meth:`~prefrontal.modules.base.Module.evaluate`
        contract): it resolves the user's current place, the local time, and the
        home-presence *transition* this tick (edge-detected against the stored
        baseline — a read here; the baseline is advanced in :meth:`after_fire`), then
        asks :func:`plan_cue_active` per plan. A firing plan becomes a single
        ``nudge`` cue deduped on ``if_then:<plan_id>`` — so the engine's debounce
        surfaces it once per cue occurrence rather than every tick the user lingers
        in the window. The ``last_fired_at`` stamp is written in :meth:`after_fire`,
        only for plans that actually pass suppression and fire.
        """
        plans = store.active_implementation_intentions()
        if not plans:
            return []
        current_place = self._current_place_name(store, ctx)
        local_dt = local_datetime(ctx.now, ctx.timezone)
        prior_presence = store.get_state(HOME_PRESENCE_STATE_KEY)
        fired_events = detect_events(prior_presence, self._current_home_presence(store, ctx))
        cues: list[Cue] = []
        for plan in plans:
            if plan_cue_active(
                plan,
                current_place_name=current_place,
                local_dt=local_dt,
                fired_events=fired_events,
            ):
                cues.append(
                    Cue(
                        module=self.key,
                        intervention="surface_at_cue",
                        urgency="nudge",
                        text=build_plan_message(plan),
                        context_key="if_then",
                        dedup_key=f"if_then:{plan['id']}",
                        ref={"plan_id": plan["id"]},
                    )
                )
        return cues

    def after_fire(
        self, store: MemoryStore, decisions: list[Decision], ctx: CoachContext
    ) -> None:
        """Advance the home-presence baseline and stamp fired plans' ``last_fired_at``.

        Two writes held back from the pure :meth:`evaluate` (the base contract):

        - **Presence baseline** — persist this tick's home presence so next tick can
          edge-detect a transition against it. Written every tick regardless of
          firing (a crossing must be caught even when no plan is cued on it), but
          only when presence is *known* — an unknown fix (no location this tick)
          leaves the last baseline intact, so a transient GPS gap isn't misread as a
          crossing when the fix returns.
        - **``last_fired_at``** — advisory (the engine's debounce, not this, prevents
          re-firing); powers the profile's "last surfaced" read. Marked on fire (past
          suppression), so a plan held by quiet hours isn't recorded as surfaced.
        """
        presence = self._current_home_presence(store, ctx)
        if presence is not None:
            store.set_state(HOME_PRESENCE_STATE_KEY, presence, source="inferred")
        stamp = ctx.now.strftime(TS_FMT)
        for d in decisions:
            if d.cue.module != self.key:
                continue
            plan_id = (d.cue.ref or {}).get("plan_id")
            if plan_id is not None:
                store.set_implementation_intention_fired(plan_id, stamp)

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize the user's active if-then plans for the behavioral profile."""
        plans = store.active_implementation_intentions()
        if not plans:
            return None
        lines = [
            f"{len(plans)} active if-then plan(s) — pre-committed cue→action pairs "
            "surfaced when their cue is detected, not on a clock."
        ]
        for plan in plans[:3]:
            cue = (plan.get("cue_text") or "").strip()
            action = (plan.get("action_text") or "").strip()
            if cue and action:
                lines.append(f"When {cue} → {action}")
        return "\n".join(f"- {line}" for line in lines)


register(ImplementationIntentionModule())
