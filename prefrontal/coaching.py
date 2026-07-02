"""Coaching agent — turn *what Prefrontal knows* into the right nudge, now.

The decision engine specced in ``docs/coaching-agent.md``: on a tick it asks every
enabled module "anything due right now?" (:meth:`Module.evaluate`), then for each
:class:`Cue` decides **whether to fire** (debounce + quiet hours), **what to
say**, and **on which channel** — returning ready-to-deliver :class:`Decision`\\s
for the delivery layer (n8n today) and leaving an audit trail so the learning
pass can calibrate future channel choice.

This module is the **pure core** — no I/O beyond the injected store, no
background threads, no transport. It generalizes the two loops that already
exist: the morning briefing's ``build → render`` phrasing shape and the outing
escalation's poll → decide-``fire`` → return-``message`` loop.

Scope of this first slice (see the spec's rollout §12): the engine, the
:class:`Cue`/:class:`CoachContext`/:class:`Decision` types, channel selection,
and suppression (quiet hours + debounce). The optional LLM phrasing pass, the
``POST /webhooks/coach/check`` endpoint, and the ``outing/check`` parity refactor
land in follow-ups; :func:`phrase` is the deterministic core those will build on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from prefrontal.scheduling import local_hour_of

# --- Urgency ladder & channel floors -----------------------------------------

#: Urgency ladder, least → most insistent. Deliberately decoupled from any one
#: module's notion of "level" so "escalation is not optional" is a property of
#: the agent, not each module (the outing ladder maps on: soft→nudge, firm→
#: urgent, call→critical).
URGENCY_LADDER = ("ambient", "nudge", "urgent", "critical")

#: Delivery channel classes, quietest → most interrupting. The agent picks a
#: *class*; the delivery layer maps it to a concrete target (Pushover/Ntfy/TTS).
CHANNEL_LADDER = ("digest", "push", "sound", "voice")

#: The channel each urgency starts at before any learned bump (spec §2 table).
URGENCY_FLOOR = {
    "ambient": "digest",   # folded into the briefing, never interrupts
    "nudge": "push",
    "urgent": "push",      # may escalate to sound if push is ignored
    "critical": "voice",   # bypasses quiet hours; always the call
}

#: Below this acknowledgement rate (with enough samples) a channel counts as
#: "ignored", so a nudge/urgent cue bumps up a rung. This is the README's
#: "which channel you ignore after 3pm" made real.
DEFAULT_IGNORE_ACK_RATE = 0.34
DEFAULT_MIN_CHANNEL_SAMPLES = 4

#: Default minutes a given cue (by ``dedup_key``) won't refire — the general
#: form of the anchor's "fire once per level" guard.
DEFAULT_DEBOUNCE_MINUTES = 120.0

#: Default responsive-hours window (local clock hours) when unset.
DEFAULT_RESPONSIVE_START = 8
DEFAULT_RESPONSIVE_END = 22


class ModuleLike(Protocol):
    """The slice of a module the engine uses — just its key and evaluator."""

    key: str

    def evaluate(self, store: Any, ctx: CoachContext) -> list[Cue]: ...


@dataclass(frozen=True)
class Cue:
    """One coaching message a module wants to deliver (spec §2)."""

    module: str            # owning module key, e.g. "location_anchor"
    intervention: str      # the declared Intervention.name, e.g. "tiny_first_step"
    urgency: str           # one of URGENCY_LADDER
    text: str              # the deterministic message (phrasing may rewrite)
    context_key: str       # for channel_response learning, e.g. "todo"/"departure"
    dedup_key: str         # stable id so a given thing fires once (debounce, §6)
    ref: dict[str, Any] = field(default_factory=dict)  # payload for outcome logging
    suggested_channel: str | None = None  # module hint; the agent may raise it


@dataclass(frozen=True)
class CoachContext:
    """Request-time facts a tick shares across modules (spec §3).

    Assembled once by :func:`build_context` so each module's ``evaluate`` doesn't
    re-read the same state.
    """

    now: datetime                       # naive UTC, like impact.utcnow()
    timezone: str = "UTC"
    display_name: str = ""              # the acting user's name, for message greetings
    responsive_start: int = DEFAULT_RESPONSIVE_START
    responsive_end: int = DEFAULT_RESPONSIVE_END
    debounce_minutes: float = DEFAULT_DEBOUNCE_MINUTES
    ignore_ack_rate: float = DEFAULT_IGNORE_ACK_RATE
    min_channel_samples: int = DEFAULT_MIN_CHANNEL_SAMPLES
    current_lat: float | None = None
    current_lon: float | None = None


@dataclass(frozen=True)
class Decision:
    """A cue the agent chose to fire, with its channel and final text."""

    cue: Cue
    channel: str
    text: str
    fire: bool = True


# --- Context assembly --------------------------------------------------------


def build_context(
    store: Any,
    *,
    now: datetime,
    timezone: str = "UTC",
    display_name: str = "",
    current_lat: float | None = None,
    current_lon: float | None = None,
) -> CoachContext:
    """Load the shared per-tick context from coaching state (spec §3)."""
    return CoachContext(
        now=now,
        timezone=timezone,
        display_name=display_name,
        responsive_start=int(store.get_float("responsive_hours_start", DEFAULT_RESPONSIVE_START)),
        responsive_end=int(store.get_float("responsive_hours_end", DEFAULT_RESPONSIVE_END)),
        debounce_minutes=store.get_float("coach_debounce_minutes", DEFAULT_DEBOUNCE_MINUTES),
        ignore_ack_rate=store.get_float("coach_ignore_ack_rate", DEFAULT_IGNORE_ACK_RATE),
        min_channel_samples=int(
            store.get_float("coach_min_channel_samples", DEFAULT_MIN_CHANNEL_SAMPLES)
        ),
        current_lat=current_lat,
        current_lon=current_lon,
    )


# --- The loop ----------------------------------------------------------------


def collect_cues(store: Any, modules: list[ModuleLike], ctx: CoachContext) -> list[Cue]:
    """Gather cues from every module; one module raising never sinks the tick.

    A module whose ``evaluate`` throws is skipped (a coaching miss beats a dead
    loop), mirroring how the registry tolerates a bad module key.
    """
    cues: list[Cue] = []
    for module in modules:
        try:
            cues.extend(module.evaluate(store, ctx))
        except Exception:  # noqa: BLE001 — isolate a bad evaluator, keep the tick alive
            continue
    return cues


def _within_hours(hour: int, start: int, end: int) -> bool:
    """Whether a local ``hour`` falls in ``[start, end)``, handling wraparound."""
    if start == end:
        return True  # a degenerate window means "always responsive"
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps past midnight


def _parse_ts(ts: Any) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _fired_key(dedup_key: str) -> str:
    """Coaching-state key holding when a cue last fired (debounce, no schema change)."""
    return f"coach_fired:{dedup_key}"


def suppressed(store: Any, cue: Cue, ctx: CoachContext) -> bool:
    """Whether a cue should be held back right now (spec §6).

    Two gates: **quiet hours** (outside responsive hours, everything but
    ``critical`` is held) and **debounce** (the same ``dedup_key`` won't refire
    within ``debounce_minutes``). ``critical`` bypasses quiet hours — a missed
    hard commitment at 6am still warrants the call — but still debounces.
    """
    if cue.urgency != "critical":
        hour = local_hour_of(ctx.now, ctx.timezone)
        if not _within_hours(hour, ctx.responsive_start, ctx.responsive_end):
            return True
    last = _parse_ts(store.get_state(_fired_key(cue.dedup_key)))
    if last is not None:
        elapsed_min = (ctx.now - last).total_seconds() / 60.0
        if elapsed_min < ctx.debounce_minutes:
            return True
    return False


def choose_channel(store: Any, cue: Cue, ctx: CoachContext) -> str:
    """Pick a delivery channel class: urgency floor, then learned bump (spec §5).

    Starts at the urgency floor, lets a module *raise* (never lower) it with a
    ``suggested_channel`` hint, then — for a nudge/urgent whose floor channel the
    user reliably ignores (low ``channel_response`` ack-rate with enough samples)
    — bumps one rung up. ``critical`` is always ``voice``.
    """
    if cue.urgency == "critical":
        return "voice"
    floor = URGENCY_FLOOR.get(cue.urgency, "push")
    hint = cue.suggested_channel
    if hint in CHANNEL_LADDER and CHANNEL_LADDER.index(hint) > CHANNEL_LADDER.index(floor):
        floor = hint
    if floor == "digest":
        return floor  # ambient never interrupts, so never bumps
    patterns = {p["context_key"]: p for p in store.get_patterns("channel_response")}
    p = patterns.get(floor)
    if (
        p is not None
        and (p.get("sample_size") or 0) >= ctx.min_channel_samples
        and (p.get("observed_value") if p.get("observed_value") is not None else 1.0)
        < ctx.ignore_ack_rate
    ):
        idx = CHANNEL_LADDER.index(floor)
        if idx < len(CHANNEL_LADDER) - 1:
            return CHANNEL_LADDER[idx + 1]
    return floor


def phrase(store: Any, cue: Cue, ctx: CoachContext) -> str:
    """Return the message to deliver.

    The deterministic core: the module's own ``cue.text``. The optional
    profile-grounded Ollama rewrite (spec §5, off for time-critical cues, with a
    heuristic fallback) is a follow-up slice and plugs in here.
    """
    return cue.text


def decide(store: Any, cues: list[Cue], ctx: CoachContext) -> list[Decision]:
    """Turn cues into fire-worthy decisions, dropping suppressed ones (spec §4)."""
    decisions: list[Decision] = []
    for cue in cues:
        if suppressed(store, cue, ctx):
            continue
        decisions.append(
            Decision(cue=cue, channel=choose_channel(store, cue, ctx), text=phrase(store, cue, ctx))
        )
    return decisions


def record_fired(store: Any, decisions: list[Decision], now: datetime) -> None:
    """Stamp each fired decision's ``dedup_key`` so debounce holds it next tick.

    Kept out of :func:`decide` so the decision logic stays pure and a ``--dry-run``
    never mutates state; the delivering caller invokes this once it commits to
    sending.
    """
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for d in decisions:
        store.set_state(_fired_key(d.cue.dedup_key), stamp, source="inferred")
