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

The engine, the :class:`Cue`/:class:`CoachContext`/:class:`Decision` types,
channel selection, and suppression (quiet hours + debounce) are implemented here;
the ``POST /webhooks/coach/check`` tick endpoint (and its CLI twin ``prefrontal
coach``) fan every enabled module through this core. :func:`phrase` now carries
the optional profile-grounded LLM rewrite (spec §5): on the opt-in
``coach_llm_phrasing`` key it warms ``ambient`` cues through the model in
Prefrontal's coaching voice, falling back to the deterministic ``cue.text`` on any
failure (time-critical nudge/urgent/critical cues stay deterministic). The
encouragement/recovery layer folds in as one more cue producer (spec §9) via
:func:`run_coaching_tick`, so its delivery routes through the same engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from prefrontal.clock import TS_FMT, local_hour_of, parse_hour
from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.log import get_logger

logger = get_logger(__name__)

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

#: Longest note we fold into a nudge before truncating — a note is a quick "don't
#: forget" hint, not a document, and an over-long one would bury the nudge itself.
NOTE_HINT_MAX_CHARS = 160

#: Urgencies whose text the optional LLM phrasing pass may rewrite. Deliberately
#: only ``ambient`` (the digest-floored, never-interrupting cues): a synchronous
#: model call on a ``nudge``/``urgent``/``critical`` path adds latency to a
#: time-critical delivery, so those keep their deterministic templates (spec §5,
#: open question §13).
PHRASING_URGENCIES = frozenset({"ambient"})

#: Coaching-state key gating the LLM phrasing pass. Off by default — the
#: deterministic ``cue.text`` is always a valid message, so phrasing is an opt-in
#: warmth upgrade, not a dependency.
PHRASING_STATE_KEY = "coach_llm_phrasing"

#: The agent name the phrasing rewrite resolves its provider under. Not a
#: :data:`~prefrontal.integrations.provider.KNOWN_AGENTS` member, so it stays on
#: the local model unless the operator opts *every* agent into Anthropic — the
#: same local-first default the encouragement prose pass uses.
PHRASING_AGENT = "coach"

#: The voice the phrasing rewrite speaks in — the shared Prefrontal register (cf.
#: the briefing/summarizer prompts), told to preserve the message's facts and only
#: warm the phrasing. The user's structured profile is appended as grounding when
#: available so the tone is calibrated to them and the numbers stay honest.
COACH_PHRASING_SYSTEM_PROMPT = (
    "You are Prefrontal's coaching voice for someone with ADHD: warm, plain, "
    "second-person, never patronizing. You are given one already-correct nudge. "
    "Rewrite it as a single short, encouraging line that keeps every fact, number, "
    "time, and name exactly as given — do not add events, tasks, or advice, and do "
    "not drop the concrete ask. One or two sentences. No preamble, headings, or "
    "emoji."
)


def note_hint(notes: str | None, *, max_chars: int = NOTE_HINT_MAX_CHARS) -> str:
    """Render a todo/commitment's notes as a suffix to fold into a nudge, or ``""``.

    The single place a stored note becomes part of a delivered nudge, so the
    departure reminder and the task-initiation nudge phrase it the same way: a
    leading separator plus ``Note: …`` (whitespace collapsed, over-long notes
    truncated to ``max_chars`` with an ellipsis). Blank/``None`` notes yield an
    empty string so the caller can unconditionally append it.

    This is why a note is worth writing — it is *consulted whenever a nudge is
    connected to the thing it annotates* ("leave now for the dentist — Note:
    bring the insurance card"), not just shown on a detail screen.
    """
    if not isinstance(notes, str):
        return ""
    text = " ".join(notes.split())
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return f" Note: {text}"


class ModuleLike(Protocol):
    """The slice of a module the engine uses — its key, evaluator, hooks, and the
    two declarations the engine's suppression/learning logic reads instead of
    naming any module (``pierces_protection`` / ``channel_targets``)."""

    key: str
    pierces_protection: bool

    def evaluate(self, store: Any, ctx: CoachContext) -> list[Cue]: ...

    def before_collect(self, store: Any, ctx: CoachContext) -> None: ...

    def after_fire(self, store: Any, decisions: list[Decision], ctx: CoachContext) -> None: ...

    def provides_protection(self, store: Any) -> bool: ...

    def channel_targets(self) -> Mapping[str, str]: ...


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
    # When True, this cue skips the **quiet-hours** gate (but still debounces and
    # still respects focus protection unless the module pierces it). For an evening
    # nudge whose whole point is to land at the edge of / outside the responsive
    # window — a "wind down for bed" cue, an end-of-day recap — where the shared
    # daytime responsive-hours window would otherwise silence it. Unlike
    # ``critical`` it does *not* force the voice channel; it's an ordinary push that
    # is simply allowed through late. A module sets this from a per-user toggle.
    quiet_hours_exempt: bool = False


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
    # Whether a module is currently shielding the user with a protective state
    # (today: an aligned hyperfocus block). Set once per tick by OR-ing every
    # enabled module's ``provides_protection`` so the suppression gate can hold
    # non-critical cues centrally rather than each module re-checking it — see
    # :func:`suppressed`.
    focus_protected: bool = False
    # The keys of enabled modules whose cues may pierce ``focus_protected`` (each
    # module declares this via ``pierces_protection``); computed once per tick so
    # the suppression gate never names a module. Empty means nothing pierces.
    pierce_keys: frozenset[str] = frozenset()


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
    focus_protected: bool = False,
    pierce_keys: frozenset[str] = frozenset(),
) -> CoachContext:
    """Load the shared per-tick context from coaching state (spec §3)."""
    return CoachContext(
        now=now,
        timezone=timezone,
        display_name=display_name,
        responsive_start=parse_hour(
            store.get_state("responsive_hours_start"), DEFAULT_RESPONSIVE_START
        ),
        responsive_end=parse_hour(
            store.get_state("responsive_hours_end"), DEFAULT_RESPONSIVE_END
        ),
        debounce_minutes=store.get_float("coach_debounce_minutes", DEFAULT_DEBOUNCE_MINUTES),
        ignore_ack_rate=store.get_float("coach_ignore_ack_rate", DEFAULT_IGNORE_ACK_RATE),
        min_channel_samples=int(
            store.get_float("coach_min_channel_samples", DEFAULT_MIN_CHANNEL_SAMPLES)
        ),
        current_lat=current_lat,
        current_lon=current_lon,
        focus_protected=focus_protected,
        pierce_keys=pierce_keys,
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
            logger.warning(
                "module %r evaluate() raised; skipping it this tick",
                getattr(module, "key", "?"),
                exc_info=True,
            )
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
    start = parse_hour(store.get_state("responsive_hours_start"), DEFAULT_RESPONSIVE_START)
    end = parse_hour(store.get_state("responsive_hours_end"), DEFAULT_RESPONSIVE_END)
    return not _within_hours(local_hour_of(now, tz), start, end)


def _fired_key(dedup_key: str) -> str:
    """Coaching-state key holding when a cue last fired (debounce, no schema change)."""
    return f"coach_fired:{dedup_key}"


def last_fired(store: Any, dedup_key: str) -> datetime | None:
    """When the cue with this ``dedup_key`` last fired, or ``None`` if never.

    The public read over the engine's debounce stamp (written by
    :func:`record_fired`). A module that paces itself on a *different* cadence than
    the engine's uniform ``debounce_minutes`` — a weekly project re-nudge, an
    item-specific check-in interval, a fire-once impulse guard — asks the engine
    "when did I last fire this?" through this accessor, rather than reaching into
    the engine's private state-key layout. Returns a naive-UTC ``datetime`` (as
    stamped) or ``None``.
    """
    return _parse_ts(store.get_state(_fired_key(dedup_key)))


def suppressed(store: Any, cue: Cue, ctx: CoachContext) -> bool:
    """Whether a cue should be held back right now (spec §6).

    Three gates: **quiet hours** (outside responsive hours, everything but
    ``critical`` is held), **protected hyperfocus** (while an aligned block is
    shielded, non-critical cues are held — except self-care, which pierces flow),
    and **debounce** (the same ``dedup_key`` won't refire within
    ``debounce_minutes``). ``critical`` bypasses quiet hours and protection — a
    missed hard commitment at 6am still warrants the call — but still debounces.

    A non-critical cue may also opt out of the **quiet-hours** gate alone by
    setting ``quiet_hours_exempt`` — for an evening nudge (wind-down, the
    end-of-day recap) whose intended moment sits at the edge of / outside the
    shared daytime responsive window. Unlike ``critical`` it doesn't escalate to
    voice or pierce protection; it's an ordinary push simply allowed through late,
    and it still debounces.
    """
    if cue.urgency != "critical":
        if not cue.quiet_hours_exempt:
            hour = local_hour_of(ctx.now, ctx.timezone)
            if not _within_hours(hour, ctx.responsive_start, ctx.responsive_end):
                return True
        # Protected hyperfocus: an aligned, healthy deep-work block shields the
        # user from *other* modules' noise. The modules that pierce it declare so
        # via ``pierces_protection`` (collected into ``ctx.pierce_keys`` once per
        # tick) — today self-care (eat / drink / meds — meant to break flow) and
        # hyperfocus's own interrupt cues (the soft alignment check is the one
        # sanctioned interrupt allowed *during* an aligned overrun, so the module
        # providing the protection must not be gated by it). This is the one
        # central gate, so a module never has to re-check it (§6).
        if ctx.focus_protected and cue.module not in ctx.pierce_keys:
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


def llm_phrasing_enabled(store: Any) -> bool:
    """Whether the opt-in LLM phrasing pass is on (:data:`PHRASING_STATE_KEY`)."""
    return (store.get_state(PHRASING_STATE_KEY, "off") or "off").strip().lower() == "on"


def phrase(
    store: Any,
    cue: Cue,
    ctx: CoachContext,
    *,
    client: Any | None = None,
    profile: str | None = None,
) -> str:
    """Return the message to deliver — deterministic by default, LLM-warmed opt-in.

    The deterministic core is the module's own ``cue.text``, always a valid
    message. When the ``coach_llm_phrasing`` key is on *and* the cue is ``ambient``
    (:data:`PHRASING_URGENCIES` — the never-interrupting, non-time-critical class),
    it's rewritten through the model in Prefrontal's coaching voice, grounded in the
    user's structured ``profile`` when one is supplied (spec §5). Any provider
    failure or empty reply falls back to ``cue.text`` — mirroring the briefing /
    encouragement prose passes — so a down model degrades quietly. Nudge/urgent/
    critical cues are never rewritten (latency on a time-critical path is bad —
    §13), so this is safe to call on every cue.

    Args:
        client: A pre-resolved model client (tests inject an offline/fake one).
            When ``None`` the provider is resolved for :data:`PHRASING_AGENT`.
        profile: The user's structured behavioral profile, appended to the system
            prompt as grounding. Built once per tick by the caller and shared
            across cues; ``None`` phrases without it.
    """
    if cue.urgency not in PHRASING_URGENCIES or not llm_phrasing_enabled(store):
        return cue.text
    from prefrontal.integrations.summarize import summarize_or_fallback

    system = COACH_PHRASING_SYSTEM_PROMPT
    if profile:
        system = (
            f"{system}\n\nThe user's behavioral profile "
            f"(for tone; obey its numbers):\n{profile}"
        )
    return summarize_or_fallback(
        cue.text, system=system, agent=PHRASING_AGENT, client=client
    ).text


def decide(
    store: Any,
    cues: list[Cue],
    ctx: CoachContext,
    *,
    client: Any | None = None,
    profile: str | None = None,
) -> list[Decision]:
    """Turn cues into fire-worthy decisions, dropping suppressed ones (spec §4).

    ``client``/``profile`` are threaded into :func:`phrase` for the optional LLM
    rewrite of ``ambient`` cues; both default to off so the deterministic path is
    unchanged.
    """
    decisions: list[Decision] = []
    for cue in cues:
        if suppressed(store, cue, ctx):
            continue
        decisions.append(
            Decision(
                cue=cue,
                channel=choose_channel(store, cue, ctx),
                text=phrase(store, cue, ctx, client=client, profile=profile),
            )
        )
    return decisions


def record_fired(store: Any, decisions: list[Decision], now: datetime) -> None:
    """Stamp each fired decision's ``dedup_key`` so debounce holds it next tick.

    Kept out of :func:`decide` so the decision logic stays pure and a ``--dry-run``
    never mutates state; the delivering caller invokes this once it commits to
    sending. This is also the single point every module-driven nudge passes
    through once we've committed to firing it, so it doubles as the ``offered``
    half of the feature-usage loop: each decision carries its owning module +
    intervention, which we record here (best-effort — a telemetry write must never
    sink the tick, exactly as the nudge log itself is fire-and-forget).
    """
    stamp = now.strftime(TS_FMT)
    for d in decisions:
        store.set_state(_fired_key(d.cue.dedup_key), stamp, source="inferred")
        try:
            store.record_feature_event(
                d.cue.module,
                "offered",
                intervention=d.cue.intervention,
                source=d.channel,
                ref=d.cue.dedup_key,
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
            logger.debug("record_feature_event(offered) failed", exc_info=True)


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

def channel_targets_for(modules: list[ModuleLike]) -> dict[str, str]:
    """Combine every enabled module's declared channel-ack targets.

    Each module declares, via
    :meth:`~prefrontal.modules.base.Module.channel_targets`, the ``context_key`` →
    ``cue.ref`` target-id key pairs whose nudges carry a tappable action button (so
    an acknowledgement is observable) — e.g. ``{"outing": "outing_id"}``. A cue
    whose context isn't in the combined map has no reliable ack signal, so it's
    never tracked; counting it would bias ``channel_response`` toward "always
    ignored". This replaces a map the engine used to hardcode, so which contexts
    are trackable now travels with the modules that emit them.
    """
    targets: dict[str, str] = {}
    for module in modules:
        targets.update(module.channel_targets())
    return targets


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


def note_delivered(
    store: Any, decisions: list[Decision], now: datetime, *, targets: Mapping[str, str]
) -> None:
    """Track each interactive nudge just fired, so its outcome can be learned.

    Only cues that carry action buttons (a ``context_key`` present in ``targets``,
    the combined per-module map from :func:`channel_targets_for`) and go out on an
    interrupting channel (not ``digest``) are tracked — a buttonless or ambient cue
    has no observable ack. Keyed by ``(context, target)``, the coordinates a
    one-tap ack arrives on (see :func:`resolve_ack`).
    """
    stamp = now.strftime(TS_FMT)
    for d in decisions:
        if d.channel == "digest":
            continue
        target = (d.cue.ref or {}).get(targets.get(d.cue.context_key, ""))
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


# --- Engagement outcomes: did a nudge move the thing it nudged? --------------
#
# The channel-outcome loop above only covers cues with a tappable ack (a
# ``context_key`` a module declares in ``channel_targets``). A module whose nudge
# has no button — "still on this project?", "heard back from your assistant?" —
# has no observable answer to a *tap*, so a self-care-style "no tap = ignored"
# sweep would mark every one of them ignored (the very bias the declared targets
# guard against). But those
# nudges do have an observable *engagement*: the thing they point at either
# advances afterward or it doesn't. These two helpers let a module stamp a fired
# nudge (``after_fire``) and later mature it into a ``success``/``ignored`` episode
# once it can tell whether the item moved (``before_collect``), with the module
# supplying its own "did it move / is it time to judge yet" verdict.


def _engage_key(module: str, target: Any) -> str:
    """Coaching-state key tracking a fired nudge awaiting an engagement verdict."""
    return f"coach_engage:{module}:{target}"


def stamp_pending_engagement(
    store: Any, decisions: list[Decision], now: datetime, *, module: str, target_key: str
) -> None:
    """Mark each fired cue of ``module`` as awaiting an engagement outcome.

    An ``after_fire`` helper. Keyed by the target id read from ``cue.ref[target_key]``;
    the first nudge in a cycle owns the window (a re-nudge on the same target before
    the verdict lands doesn't reset the clock), mirroring how ``note_delivered``
    stamps once per ``(context, target)``.
    """
    stamp = now.strftime(TS_FMT)
    for d in decisions:
        if d.cue.module != module:
            continue
        target = (d.cue.ref or {}).get(target_key)
        if target is None:
            continue
        key = _engage_key(module, target)
        if store.get_state(key) is None:
            store.set_state(key, stamp, source="inferred")


def sweep_engagement(
    store: Any,
    now: datetime,
    *,
    module: str,
    episode_type: str,
    verdict: Any,
) -> int:
    """Mature pending engagement markers for ``module`` into outcome episodes.

    A ``before_collect`` helper. For each marker, calls
    ``verdict(target: str, fired: datetime) -> "engaged" | "ignored" | None``; a
    ``None`` verdict means "not yet — give it more time" and leaves the marker. A
    decided marker is logged as an ``episode_type`` episode (``success`` +
    ``acknowledged`` when engaged, ``ignored`` otherwise) and cleared. Returns how
    many were resolved. Mirrors :func:`sweep_stale_nudges`' shape.
    """
    swept = 0
    prefix = f"coach_engage:{module}:"
    for key, row in list(store.all_state().items()):
        if not key.startswith(prefix):
            continue
        raw = (row.get("value") if isinstance(row, dict) else row) or ""
        fired = _parse_ts(str(raw))
        if fired is None:
            store.delete_state(key)  # malformed marker — drop it
            continue
        target = key[len(prefix) :]
        decided = verdict(target, fired)
        if decided is None:
            continue  # too soon to judge — leave the marker for a later tick
        engaged = decided == "engaged"
        store.log_episode(
            episode_type,
            acknowledged=engaged,
            context=f"{module} nudge: {target}",
            outcome="success" if engaged else "ignored",
        )
        store.delete_state(key)
        swept += 1
    return swept


@dataclass(frozen=True)
class TickResult:
    """The outcome of one coaching tick (see :func:`run_coaching_tick`).

    Attributes:
        decisions: The fire-worthy decisions, already recorded/marked.
        cues: Every cue collected this tick (a superset of ``decisions``' cues —
            some were held by suppression). Callers report ``len(cues)`` as "held".
        swept_stale: Unanswered nudges logged as channel misses this tick.
        decomposed: Avoided tasks broken down into a first step this tick.
        clarified: Newly-ambiguous items filed as clarifying questions this tick.
        encouraged: Whether a rough-day recovery cue fired this tick (§9).
    """

    decisions: list[Decision]
    cues: list[Cue]
    swept_stale: int
    decomposed: int
    clarified: int
    encouraged: bool = False


def _run_module_hooks(
    modules: list[ModuleLike], run: Any, phase: str
) -> None:
    """Run a per-module lifecycle hook, isolating a raising module.

    Mirrors :func:`collect_cues`' robustness: one module's ``before_collect`` /
    ``after_fire`` blowing up is logged and skipped rather than sinking the tick.
    """
    for module in modules:
        try:
            run(module)
        except Exception:  # noqa: BLE001 — isolate a bad hook, keep the tick alive
            logger.warning(
                "module %r %s hook raised; skipping it this tick",
                getattr(module, "key", "?"),
                phase,
                exc_info=True,
            )


def _any_protection(modules: list[ModuleLike], store: Any) -> bool:
    """Whether any enabled module is currently shielding the user (isolated).

    OR-s each module's ``provides_protection``; a module that raises is skipped
    (as in :func:`collect_cues`) so a broken protector can't sink the tick — it
    just fails open, dropping *its* shield rather than silencing every cue.
    """
    for module in modules:
        try:
            if module.provides_protection(store):
                return True
        except Exception:  # noqa: BLE001 — isolate a bad protector, keep the tick alive
            logger.warning(
                "module %r provides_protection() raised; ignoring its shield this tick",
                getattr(module, "key", "?"),
                exc_info=True,
            )
    return False


def _safe_sweep(label: str, run: Any) -> int:
    """Run a pre-collection input-refresh sweep, isolating a failure to 0.

    The decomposition / clarification refreshes run *before* the guarded
    :func:`collect_cues`, so without this a raise in either would sink the whole
    tick — the very outcome the per-module isolation elsewhere prevents.
    """
    try:
        return int(run())
    except Exception:  # noqa: BLE001 — a bad refresh sweep must not sink the tick
        logger.warning("%s sweep raised; skipping it this tick", label, exc_info=True)
        return 0


def run_coaching_tick(
    store: Any,
    *,
    settings: Any,
    ollama: Any,
    now: datetime | None = None,
    current_lat: float | None = None,
    current_lon: float | None = None,
    display_name: str = "",
) -> TickResult:
    """Run one coaching tick: close prior outcomes, refresh inputs, decide, record.

    The single orchestration path behind ``POST /webhooks/coach/check`` and
    ``prefrontal coach [--deliver]`` (the general form of the anchor's fire-once
    guard). In order:

    1. Sweep last round's unanswered nudges into channel *misses* (engine-native
       channel learning, spec §8).
    2. Refresh avoided-task decompositions and ambiguity questions **before**
       collecting cues, so ``tiny_first_step`` uses the fresh first step and the
       clarify queue fills passively. Bounded model calls; no-ops when nothing's
       due.
    3. Run each enabled module's :meth:`~prefrontal.modules.base.Module.before_collect`
       hook (its own pre-collection housekeeping — e.g. self-care sweeps its
       unanswered nudges into "ignored" episodes), collect every module's cues,
       decide channel + suppression, record the fires, then run each module's
       :meth:`~prefrontal.modules.base.Module.after_fire` hook (e.g. self-care
       stamps its delivery time).

    The engine never names a specific module: protection (which module is
    shielding the user), which cues may pierce it, and which contexts get
    channel-ack tracking are all read off the modules themselves
    (``provides_protection`` / ``pierces_protection`` / ``channel_targets``), and
    every module-facing step is failure-isolated so one bad module never sinks the
    tick. (The step-2 refreshes are generic input functions, not module methods,
    and are isolated by :func:`_safe_sweep`.)

    This owns *which steps constitute a tick* and their order, so the endpoint and
    CLI callers can't drift. The callers only format or deliver the returned
    decisions.

    Args:
        store: A store scoped to the user being coached.
        settings: Operator settings (timezone).
        ollama: The model client for the decomposition / clarification sweeps.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        current_lat: Optional latitude for location-aware evaluators.
        current_lon: Optional longitude for location-aware evaluators.
        display_name: The user's display name, threaded into the context.

    Returns:
        A :class:`TickResult`.
    """
    from prefrontal.clarify import sweep_ambiguous_items
    from prefrontal.impact import utcnow
    from prefrontal.modules import enabled_modules
    from prefrontal.todos import sweep_avoided_decompositions

    now = now or utcnow()
    modules = enabled_modules(settings)
    # Per-user mute (the usage loop's "act on it" half): a module the user muted
    # from the weekly usage nudge is dropped from the whole tick — it offers no
    # cues, provides no protection, and records no `offered` events — without
    # touching global config. Best-effort read so a store without the repo (older
    # test doubles) still runs the tick.
    try:
        muted = store.muted_features()
    except Exception:  # noqa: BLE001 — mute is a convenience, never a tick blocker
        logger.warning("muted_features() read failed; running with none muted", exc_info=True)
        muted = set()
    if muted:
        modules = [m for m in modules if m.key not in muted]
    # Close last round's channel outcomes (taps clear their own markers, so what's
    # swept really went unanswered). Engine-native; a module's own pre-collection
    # housekeeping runs through its before_collect hook below.
    swept_stale = sweep_stale_nudges(
        store,
        now,
        ack_window_minutes=store.get_float(
            "coach_ack_window_minutes", DEFAULT_ACK_WINDOW_MINUTES
        ),
    )
    # Refresh model-derived inputs *before* collecting cues so tiny_first_step and
    # the clarify queue see fresh state. Isolated: these run ahead of the guarded
    # collect, so a raise here must not sink the tick.
    decomposed = _safe_sweep(
        "decomposition", lambda: sweep_avoided_decompositions(store, ollama, now=now)
    )
    clarified = _safe_sweep(
        "clarification", lambda: len(sweep_ambiguous_items(store, ollama))
    )
    ctx = build_context(
        store,
        now=now,
        timezone=settings.timezone,
        display_name=display_name,
        current_lat=current_lat,
        current_lon=current_lon,
        # Protection and its piercers are read off the modules themselves, so the
        # engine names none of them (both isolated against a raising module).
        focus_protected=_any_protection(modules, store),
        pierce_keys=frozenset(m.key for m in modules if m.pierces_protection),
    )
    _run_module_hooks(modules, lambda m: m.before_collect(store, ctx), "before_collect")
    cues = collect_cues(store, modules, ctx)
    # The encouragement/recovery layer is one more cue producer (spec §9), not a
    # separate system: assess the day and, when it's rough, emit a single recovery
    # cue that routes through the *same* channel choice, debounce, and quiet-hours
    # gate as everything else. Isolated like a module evaluator so a bad assessment
    # never sinks the tick.
    cues.extend(_encouragement_cues(store, ctx))
    # Build the structured profile once (not per cue) only when the opt-in phrasing
    # pass could actually use it — on, and something ambient to warm.
    profile = _phrasing_profile(store, cues)
    decisions = decide(store, cues, ctx, profile=profile)
    # Recorded at decision time (not on confirmed delivery), matching how
    # outing/check advances last_level when it decides to fire.
    record_fired(store, decisions, now)
    note_delivered(store, decisions, now, targets=channel_targets_for(modules))
    encouraged = _mark_encouragement_if_fired(store, decisions, now)
    _run_module_hooks(modules, lambda m: m.after_fire(store, decisions, ctx), "after_fire")
    return TickResult(
        decisions=decisions,
        cues=cues,
        swept_stale=swept_stale,
        decomposed=decomposed,
        clarified=clarified,
        encouraged=encouraged,
    )


def _phrasing_profile(store: Any, cues: list[Cue]) -> str | None:
    """The structured profile to ground LLM phrasing, or ``None`` when it can't help.

    Built once per tick and shared across cues (see :func:`phrase`). Skipped
    entirely — no profile read — unless the phrasing pass is on and at least one
    cue is in :data:`PHRASING_URGENCIES`, so the deterministic path pays nothing.
    """
    if not llm_phrasing_enabled(store):
        return None
    if not any(c.urgency in PHRASING_URGENCIES for c in cues):
        return None
    from prefrontal.memory.summarizer import build_profile

    try:
        return build_profile(store)
    except Exception:  # noqa: BLE001 — grounding is best-effort; phrase without it
        logger.warning(
            "build_profile for coach phrasing raised; phrasing ungrounded", exc_info=True
        )
        return None


def _encouragement_cues(store: Any, ctx: CoachContext) -> list[Cue]:
    """Collect the encouragement layer's recovery cue, isolating any failure."""
    try:
        from prefrontal.encouragement import encouragement_cues

        return encouragement_cues(store, ctx)
    except Exception:  # noqa: BLE001 — a bad assessment must not sink the tick
        logger.warning("encouragement_cues raised; skipping it this tick", exc_info=True)
        return []


def _mark_encouragement_if_fired(store: Any, decisions: list[Decision], now: datetime) -> bool:
    """Advance the once-per-day cursor iff a recovery cue actually fired this tick.

    Marked on *fire* (past suppression), not on production — a recovery cue held by
    quiet hours should re-offer once the window opens, exactly as the old
    ``/encouragement`` → ``/encouragement/sent`` contract only stamped after a real
    delivery. Returns whether it fired.
    """
    from prefrontal.encouragement import ENCOURAGEMENT_MODULE, mark_sent_today

    if not any(d.cue.module == ENCOURAGEMENT_MODULE for d in decisions):
        return False
    mark_sent_today(store, now=now)
    return True
