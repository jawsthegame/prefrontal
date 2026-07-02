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


def in_quiet_hours(store: Any, now: datetime, tz: str) -> bool:
    """Whether local time is outside the user's responsive hours (spec §6).

    The same quiet-hours window :func:`suppressed` gates non-critical cues on,
    factored out so other proactive pollers (panic, encouragement) can defer to
    responsive hours without assembling a full :class:`CoachContext`. Reads the
    per-user ``responsive_hours_start`` / ``responsive_hours_end`` state, like
    :func:`build_context`.
    """
    start = int(store.get_float("responsive_hours_start", DEFAULT_RESPONSIVE_START))
    end = int(store.get_float("responsive_hours_end", DEFAULT_RESPONSIVE_END))
    return not _within_hours(local_hour_of(now, tz), start, end)


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


# --- Outcome loop: learn which channels actually land (spec §8) --------------
#
# ``choose_channel`` bumps a cue up a rung when the user reliably *ignores* a
# channel — but that only works if something feeds the ``channel_response``
# patterns ``patterns.py`` derives (ack-rate per channel). These helpers close
# that loop: a delivered interactive nudge is tracked by the (context, target)
# coordinates a one-tap ack arrives on; a tap records an *acknowledged* outcome,
# and a nudge that goes unanswered past a window is swept into a *miss*. Both
# feed ``channel_response``, so the nightly ``learn`` shifts future channel
# choice. No new learning code — just the episodes the deriver already consumes.

#: Minutes a delivered nudge waits for a tap before it's counted as ignored.
DEFAULT_ACK_WINDOW_MINUTES = 60.0

#: Contexts whose nudges carry tappable action buttons (so an acknowledgement is
#: observable), mapped to the ``cue.ref`` key holding the target id. A cue whose
#: context isn't here has no reliable ack signal, so it's never tracked — counting
#: it would bias ``channel_response`` toward "always ignored".
_TRACKABLE_TARGET = {"outing": "outing_id", "departure": "commitment_id", "focus": "session_id"}


def _ack_key(context: str, target: Any) -> str:
    """Coaching-state key tracking a delivered nudge awaiting its outcome."""
    return f"coach_ack:{context}:{target}"


def record_channel_outcome(
    store: Any, *, channel: str, context: str, acknowledged: bool
) -> None:
    """Log one delivered nudge's fate as a ``channel_response``-feeding episode.

    ``patterns.py`` groups episodes by ``channel`` and averages ``acknowledged``
    into a per-channel ack-rate; ``choose_channel`` reads that back. The channel
    is the engine's class (``push``/``sound``/``voice``) so it lines up with the
    floor ``choose_channel`` looks up.
    """
    store.log_episode(
        "reminder",
        channel=channel,
        acknowledged=acknowledged,
        context=f"coach nudge: {context}",
        outcome="success" if acknowledged else "miss",
    )


def note_delivered(store: Any, decisions: list[Decision], now: datetime) -> None:
    """Track each interactive nudge just fired, so its outcome can be learned.

    Only cues that carry action buttons (:data:`_TRACKABLE_TARGET`) and go out on
    an interrupting channel (not ``digest``) are tracked — a buttonless or ambient
    cue has no observable ack. Keyed by ``(context, target)``, the coordinates a
    one-tap ack arrives on (see :func:`resolve_ack`).
    """
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for d in decisions:
        if d.channel == "digest":
            continue
        target = (d.cue.ref or {}).get(_TRACKABLE_TARGET.get(d.cue.context_key, ""))
        if target is None:
            continue
        store.set_state(
            _ack_key(d.cue.context_key, target),
            f"{stamp}|{d.channel}|{d.cue.context_key}",
            source="inferred",
        )


def resolve_ack(
    store: Any, context: str, target: Any, *, acknowledged: bool = True
) -> bool:
    """Resolve a tracked nudge on a tap: log its channel outcome, clear tracking.

    Called from the one-tap action path (``/nudge/act``) with the same
    ``(context, target)`` :func:`note_delivered` stamped. Returns ``True`` when a
    tracked nudge was found (and thus an outcome logged), ``False`` otherwise — so
    a tap on a nudge the engine didn't originate is a harmless no-op.
    """
    raw = store.get_state(_ack_key(context, target))
    if not raw:
        return False
    parts = str(raw).split("|")
    channel = parts[1] if len(parts) > 1 else ""
    if channel:
        record_channel_outcome(store, channel=channel, context=context, acknowledged=acknowledged)
    store.delete_state(_ack_key(context, target))
    return True


def sweep_stale_nudges(
    store: Any, now: datetime, *, ack_window_minutes: float = DEFAULT_ACK_WINDOW_MINUTES
) -> int:
    """Count nudges unanswered past the window as channel *misses*, and clear them.

    Run once per tick: any tracked nudge older than ``ack_window_minutes`` that
    was never tapped becomes an ``acknowledged=False`` ``channel_response`` episode
    (the "you ignored this channel" signal), then its marker is cleared. Returns
    how many were swept. Safe because taps clear acknowledged nudges first
    (:func:`resolve_ack`), so what's left really is unanswered.
    """
    swept = 0
    for key, row in store.all_state().items():
        if not key.startswith("coach_ack:"):
            continue
        raw = (row.get("value") if isinstance(row, dict) else row) or ""
        parts = str(raw).split("|")
        ts = _parse_ts(parts[0]) if parts and parts[0] else None
        if ts is None:
            store.delete_state(key)  # malformed marker — drop it
            continue
        if (now - ts).total_seconds() / 60.0 < ack_window_minutes:
            continue  # still within the ack window — leave it for now
        channel = parts[1] if len(parts) > 1 else ""
        context = parts[2] if len(parts) > 2 else ""
        if channel:
            record_channel_outcome(store, channel=channel, context=context, acknowledged=False)
        store.delete_state(key)
        swept += 1
    return swept
