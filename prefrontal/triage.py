"""Triage core — classify an inbound signal into a routable decision.

The source-agnostic *classify* step from the README architecture ("classifies,
prioritizes, routes"), specced in :doc:`docs/triage-agent.md`. Given a normalized
:class:`Signal` — an email, a calendar change, an n8n event, a manual capture —
it decides three things:

1. **kind** — what is this? (:data:`KINDS`)
2. **urgency** — how soon does it matter? (:data:`URGENCIES`)
3. **route** — where does it go? (:data:`ROUTES`, derived from ``kind``)

This module is the **pure, model-optional core** only: :func:`classify` and its
two layers. The *apply* step (writing to the store / firing an n8n event) and the
HTTP wiring land in later slices — nothing here touches the store, so importing it
changes no existing behavior.

The classifier follows the exact two-layer pattern :func:`prefrontal.todos.augment_todo`
uses: a deterministic :func:`_heuristic_classify` that always runs, and — only for
the genuinely ambiguous (confidence below :data:`HEURISTIC_TRUST`) — one JSON call
to :func:`_llm_classify`, which falls back to the heuristic on any model failure.
So the model being off never drops a signal (local-first), and tests pin the
heuristic contract with the model off *and* the refinement path with a fake
generator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from prefrontal.integrations import Generator, OllamaError
from prefrontal.llm_json import extract_json_object

#: What a signal is. Maps onto the memory layer triage routes into (see ROUTE_FOR_KIND).
KINDS: tuple[str, ...] = ("commitment", "action", "outcome", "preference", "info", "noise")

#: How soon a signal matters — decoupled from kind (a far-off commitment is "later").
URGENCIES: tuple[str, ...] = ("now", "today", "later", "none")

#: The side effect apply performs. Derived from kind, never model-chosen.
ROUTES: tuple[str, ...] = ("commitment", "todo", "episode", "state", "surface", "drop")

#: kind → route. The routing table (spec §5); triage adds no new storage primitives.
ROUTE_FOR_KIND: dict[str, str] = {
    "commitment": "commitment",
    "action": "todo",
    "outcome": "episode",
    "preference": "state",
    "info": "surface",
    "noise": "drop",
}

#: At/above this heuristic confidence, skip the model — the cheap path is trusted.
HEURISTIC_TRUST = 0.75

_WEEKDAYS = {
    d: i for i, d in enumerate(
        ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    )
}

# --- keyword rules (whole-word matched; order in classify is most-specific-first) --
#: Sender fragments / list headers that mark automated bulk mail.
_NOISE_SENDERS = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "notifications@", "newsletter@", "bounce", "postmaster",
)
_NOISE_WORDS = (
    "unsubscribe", "newsletter", "receipt", "your order", "order confirmation",
    "shipped", "out for delivery", "sale", "% off", "deal", "digest",
    "verify your email", "weekly summary", "promotional",
)
_PREFERENCE_WORDS = (
    "stop texting me", "stop reminding me", "stop nudging me", "stop emailing me",
    "don't remind me", "dont remind me", "don't nudge me", "i prefer",
    "i'd prefer", "no notifications", "quiet hours", "leave me alone",
)
_OUTCOME_WORDS = (
    "i left", "i made it", "made it", "i missed", "missed it", "i finished",
    "i was late", "got there", "i arrived", "i skipped", "i forgot to",
)
_COMMITMENT_WORDS = (
    "appointment", "appt", "meeting", "confirmed", "confirmation", "flight",
    "reservation", "booked", "rsvp", "scheduled for", "calendar invite", "invite",
    "your booking", "webinar", "call with",
)
_ACTION_WORDS = (
    "please", "due", "overdue", "asap", "action required", "action needed",
    "reminder", "don't forget", "need to", "can you", "follow up", "respond",
    "reply", "submit", "renew", "pay", "sign", "review", "complete", "send",
)
_NOW_WORDS = ("overdue", "asap", "urgent", "immediately", "right now", "past due", "final notice")
_TODAY_WORDS = ("today", "tonight", "eod", "end of day", "this afternoon", "this morning")
_TIME_RE = re.compile(r"\b(?:1[0-2]|0?[1-9])(?::[0-5][0-9])?\s*(?:am|pm)\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}\b")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    """Whole-word/phrase membership ('call' won't match 'recall')."""
    return any(re.search(rf"\b{re.escape(p)}\b", text) for p in phrases)


@dataclass(frozen=True)
class Signal:
    """A normalized inbound event. Ingestion adapters build it; triage consumes it."""

    source: str            # "mail" | "calendar" | "shortcut" | "n8n" | "manual"
    title: str             # subject line / event title / short capture
    body: str = ""         # email body, event notes, etc. (may be empty)
    sender: str = ""       # from-address / origin, for sender-trust heuristics
    received_at: str = ""  # ISO8601; defaults to utcnow() at apply time
    external_id: str = ""  # provider id for idempotency (e.g. gmail msg id)
    meta: dict[str, Any] = field(default_factory=dict)  # raw extras, untyped


@dataclass(frozen=True)
class TriageDecision:
    """The classifier's verdict. ``reason`` is always populated — triage never
    silently eats input."""

    kind: str
    urgency: str
    route: str
    reason: str
    confidence: float
    source: str            # "heuristic" | "llm" — which path decided
    fields: dict[str, Any] = field(default_factory=dict)  # extracted slots (when, deadline)


def _when_from_text(text: str, today: date) -> str | None:
    """Parse a relative day reference ('tomorrow', 'by Friday') → ISO date, or None.

    Mirrors :func:`prefrontal.todos.heuristic_deadline` (its weekday math is more
    reliable than a model's date arithmetic).
    """
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()
    if _has(text, _TODAY_WORDS):
        return today.isoformat()
    if "next week" in text:
        return (today + timedelta(days=7)).isoformat()
    m = re.search(
        r"\b(?:by|on|before|due|for)\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    ) or re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text)
    if m:
        delta = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).isoformat()
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    return iso.group(1) if iso else None


def _urgency_for(kind: str, text: str, when: str | None, today: date) -> str:
    """Map wording + parsed date to an urgency (spec §4.2)."""
    if kind in ("outcome", "preference", "info", "noise"):
        return "none"
    if _has(text, _NOW_WORDS):
        return "now"
    if when is not None:
        if when <= today.isoformat():
            return "now" if kind == "action" else "today"
        return "later"
    if _has(text, _TODAY_WORDS):
        return "today"
    # A dateless action still wants attention today; a dateless commitment is vague.
    return "today" if kind == "action" else "later"


def _heuristic_classify(signal: Signal, *, today: date) -> TriageDecision:
    """Deterministic keyword/structural classifier — always runs, always testable."""
    text = f"{_norm(signal.title)} {_norm(signal.body)}".strip()
    sender = _norm(signal.sender)
    meta_blob = _norm(
        " ".join(f"{k} {v}" for k, v in signal.meta.items())
    ) if signal.meta else ""

    # 1. Noise: automated senders / bulk list headers / marketing subject cues.
    listy = "list-unsubscribe" in meta_blob or "bulk" in meta_blob
    if listy or any(s in sender for s in _NOISE_SENDERS) or _has(text, _NOISE_WORDS):
        conf = 0.9 if (listy or any(s in sender for s in _NOISE_SENDERS)) else 0.78
        return _decide("noise", "none", "automated / bulk sender or marketing cue", conf)

    # 2. Preference: the user telling us how to coach them.
    if _has(text, _PREFERENCE_WORDS):
        return _decide("preference", "none", "stated coaching preference", 0.8)

    # 3. Outcome: first-person evidence about something already predicted.
    if _has(text, _OUTCOME_WORDS):
        return _decide("outcome", "none", "first-person outcome report", 0.8)

    has_when = bool(_TIME_RE.search(text) or _DATE_RE.search(text)) or _when_from_text(text, today)
    when = _when_from_text(text, today)

    # 4. Commitment: event words + a date/time signal.
    if _has(text, _COMMITMENT_WORDS) and has_when:
        u = _urgency_for("commitment", text, when, today)
        d = _decide("commitment", u, "event language with a date/time", 0.8)
        return _with_when(d, when)

    # 5. Action: imperative / due language.
    if _has(text, _ACTION_WORDS):
        u = _urgency_for("action", text, when, today)
        strong = _has(text, _NOW_WORDS) or when is not None
        d = _decide("action", u, "actionable request", 0.85 if strong else 0.6)
        return _with_when(d, when)

    # 6. Bare commitment word without a date, or nothing decisive → low-confidence.
    if _has(text, _COMMITMENT_WORDS):
        return _decide("commitment", "later", "event language, no clear date", 0.55)
    return _decide("info", "none", "no actionable signal detected", 0.3)


def _decide(kind: str, urgency: str, reason: str, confidence: float) -> TriageDecision:
    return TriageDecision(
        kind=kind, urgency=urgency, route=ROUTE_FOR_KIND[kind],
        reason=reason, confidence=round(confidence, 2), source="heuristic",
    )


def _with_when(decision: TriageDecision, when: str | None) -> TriageDecision:
    """Attach a parsed ``when`` to a decision's ``fields`` (no-op when None)."""
    if when is None:
        return decision
    return TriageDecision(
        kind=decision.kind, urgency=decision.urgency, route=decision.route,
        reason=decision.reason, confidence=decision.confidence, source=decision.source,
        fields={**decision.fields, "when": when},
    )


_LLM_SYSTEM = (
    "You triage one inbound signal for a personal ADHD assistant. Reply with ONLY "
    "a JSON object, no prose: {\"kind\": one of "
    "commitment|action|outcome|preference|info|noise, \"urgency\": one of "
    "now|today|later|none, \"reason\": \"<one short phrase>\", \"when\": "
    "\"YYYY-MM-DD\" or null}. kind: commitment = a dated event; action = an open "
    "task; outcome = the user reporting what happened; preference = how they want "
    "to be coached; info = worth seeing once, no action; noise = newsletters/"
    "receipts/automated cruft. urgency: now = nudge in minutes; today = in today's "
    "plan; later = future/low; none = nothing to schedule."
)


def _llm_classify(signal: Signal, client: Generator, *, today: date) -> TriageDecision | None:
    """One JSON call refining the ambiguous cases; None on any model failure.

    Coerced/validated like :func:`prefrontal.todos._coerce_llm` — an unknown
    ``kind``/``urgency``, malformed JSON, or :class:`OllamaError` all yield
    ``None`` so the heuristic wins (local-first, never a dropped signal).
    """
    prompt = (
        f"Source: {signal.source}\nFrom: {signal.sender}\nToday: {today.isoformat()}\n"
        f"Subject: {signal.title}\nBody: {signal.body[:1500]}"
    )
    try:
        reply = client.generate(prompt, system=_LLM_SYSTEM)
    except OllamaError:
        return None
    raw = extract_json_object(reply)
    kind = raw.get("kind")
    urgency = raw.get("urgency")
    if kind not in KINDS or urgency not in URGENCIES:
        return None
    raw_reason = raw.get("reason")
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else "model classification"
    )
    fields: dict[str, Any] = {}
    when = raw.get("when")
    if isinstance(when, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", when.strip()):
        fields["when"] = when.strip()
    return TriageDecision(
        kind=kind, urgency=urgency, route=ROUTE_FOR_KIND[kind],
        reason=reason[:200], confidence=0.9, source="llm", fields=fields,
    )


def classify(
    signal: Signal,
    *,
    client: Generator | None = None,
    today: date | None = None,
    drop_threshold: float = 0.0,
) -> TriageDecision:
    """Classify a signal, using the model only for the ambiguous cases.

    Args:
        signal: The normalized inbound signal.
        client: Optional LLM client; when ``None`` the pure heuristic decides.
        today: Injectable clock for date math (defaults to ``date.today()``).
        drop_threshold: When a ``noise`` verdict is *less* confident than this,
            surface it instead of dropping (route ``surface``) — so uncertain
            "noise" is seen, not silently discarded. Default ``0.0`` keeps every
            noise verdict a drop.

    Returns:
        A :class:`TriageDecision`; ``route`` always follows ``kind``.
    """
    today = today or date.today()
    base = _heuristic_classify(signal, today=today)
    decision = base
    if client is not None and base.confidence < HEURISTIC_TRUST:
        decision = _llm_classify(signal, client, today=today) or base
    # Low-confidence noise is surfaced rather than dropped (spec §8).
    if decision.kind == "noise" and decision.confidence < drop_threshold:
        decision = TriageDecision(
            kind="noise", urgency="none", route="surface",
            reason=decision.reason + " (low confidence — surfaced)",
            confidence=decision.confidence, source=decision.source, fields=decision.fields,
        )
    return decision
