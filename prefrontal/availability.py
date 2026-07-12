"""Freeform "find me a time" scheduling assistant.

The dashboard/CLI lets you say *"find 45 minutes for coffee with Sam this week"*
or *"when are my wife and I both free for dinner tomorrow evening?"* and get back
a short list of open slots — or a single clarifying question when the ask is too
vague to answer. This module is the brain behind that.

It is deliberately a thin, **participant-aware** layer over the existing
slot-finder (:func:`prefrontal.scheduling.find_slots`), in three steps:

1. **Interpret.** :func:`interpret_request` turns free text into a validated
   :class:`ScheduleRequest` — duration, how many days to scan, an optional
   time-of-day band, whether a partner is involved. It prefers the configured
   model (Claude/Ollama) and falls back to an offline heuristic parse, so it
   still works with no model reachable. When it can't nail down a *time* (most
   often: no duration given) it returns a **clarifying question** instead of
   guessing.
2. **Constrain by who's involved.** :func:`constraint_commitments` decides which
   calendar entries actually *block* the search. Your own commitments always do.
   A partner's whereabouts — the *FYI* events (``kind == "fyi"``; "where someone
   else will be", see :mod:`prefrontal.commitments`) — block **only** when the
   plan involves that partner. So "just me" ignores items that are only your
   wife's, while "the two of us" treats her calendar as a hard constraint too.
3. **Find slots.** :func:`find_availability` subtracts those constraints across
   the waking band of the coming days and returns the open windows.

:func:`plan_availability` chains all three against a scoped memory store and
returns an :class:`AvailabilityPlan` — either a question, or the request echo
plus the slots — that a router or the CLI renders with :func:`render_plan`.

Nothing here writes: it is pure read + compute, safe to poll.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prefrontal.clock import local_datetime, parse_ts, utcnow
from prefrontal.commitments import KIND_FYI, is_attendable
from prefrontal.llm_json import generate_json
from prefrontal.log import get_logger
from prefrontal.scheduling import (
    DEFAULT_SLOT_DAYS,
    DEFAULT_SLOT_LIMIT,
    FreeWindow,
    find_slots,
    parse_window,
)

_log = get_logger(__name__)

#: Bounds on a requested slot length, in minutes — 5 minutes to a full day. Wide
#: enough for any real meeting while rejecting a nonsense value the model coughs up.
MIN_REQUEST_MINUTES = 5.0
MAX_REQUEST_MINUTES = 24 * 60.0

#: Largest horizon we'll scan, in local days (mirrors the ``/calendar/slots`` cap).
MAX_REQUEST_DAYS = 14

#: Words that signal the plan involves a partner/spouse (so their calendar — the
#: FYI events — becomes a hard constraint). Matched case-insensitively on word
#: boundaries. "date night" is caught separately (it spans a space).
_PARTNER_WORDS = frozenset({
    "wife", "husband", "spouse", "partner", "we", "us", "our", "ours",
    "together", "both",
})

#: Named time-of-day bands (local ``HH:MM``), so "in the morning" / "tomorrow
#: evening" narrows the search without an explicit clock time.
_TIME_OF_DAY = {
    "morning": ("06:00", "12:00"),
    "afternoon": ("12:00", "17:00"),
    "evening": ("17:00", "22:00"),
    "tonight": ("17:00", "22:00"),
    "night": ("17:00", "22:00"),
    "lunch": ("11:30", "13:30"),
    "lunchtime": ("11:30", "13:30"),
}


@dataclass(frozen=True)
class ScheduleRequest:
    """A parsed, validated "find me a time" ask.

    Attributes:
        minutes: Slot length to look for (``MIN_REQUEST_MINUTES`` ≤ m ≤
            ``MAX_REQUEST_MINUTES``).
        days: How many local days ahead to scan, starting today (1–14).
        with_partner: Whether the plan involves a partner — when ``True`` their
            FYI events become hard constraints (see :func:`constraint_commitments`).
        title: A short label for what's being scheduled ("coffee with Sam"), or
            ``None`` if the text didn't say.
        time_window: An optional local ``("HH:MM", "HH:MM")`` band to restrict the
            search to (e.g. afternoons only); ``None`` uses the full waking band.
    """

    minutes: float
    days: int = DEFAULT_SLOT_DAYS
    with_partner: bool = False
    title: str | None = None
    time_window: tuple[str, str] | None = None


@dataclass
class AvailabilityPlan:
    """The result of a "find me a time" ask.

    Exactly one of ``question`` / ``slots`` is the payload: when the ask is too
    vague to answer, ``question`` is set (and ``request`` / ``slots`` are empty);
    otherwise ``request`` echoes what we understood and ``slots`` holds the open
    windows.

    Attributes:
        question: A single clarifying question to put back to the user, or ``None``.
        request: The interpreted request (``None`` when we're asking a question).
        slots: Open windows that fit, soonest first (empty when asking, or when
            genuinely nothing fits).
        with_partner: Convenience mirror of ``request.with_partner``.
        considered: How many commitments were treated as blocking constraints.
        ignored_fyi: How many FYI events were *ignored* because no partner was
            involved — surfaced so the reply can say "ignoring 2 of your wife's
            items" and the user can correct us.
    """

    question: str | None = None
    request: ScheduleRequest | None = None
    slots: list[FreeWindow] = field(default_factory=list)
    with_partner: bool = False
    considered: int = 0
    ignored_fyi: int = 0


# --- who's involved → which commitments block ------------------------------


def constraint_commitments(
    commitments: list[dict[str, Any]], *, with_partner: bool
) -> tuple[list[dict[str, Any]], int]:
    """Partition ``commitments`` into what blocks the search and count the rest.

    Your own real commitments always block — :func:`is_attendable` keeps the
    events you personally attend and drops placeholder/hold blocks (elastic time
    you'd yield). A **partner's** commitments show up as FYI events ("where
    someone else will be"); they block **only** when ``with_partner`` is set:

    - ``with_partner=False`` (just you): FYI events are ignored — an appointment
      that's only your wife's doesn't stop *you* from being free.
    - ``with_partner=True`` (the two of you): FYI events block too, because the
      slot has to be open for both of you.

    Returns ``(blocking, ignored_fyi)`` where ``ignored_fyi`` is how many FYI
    events were left out (always 0 when ``with_partner`` is set).
    """
    blocking: list[dict[str, Any]] = []
    ignored_fyi = 0
    for c in commitments:
        if c.get("kind") == KIND_FYI:
            if with_partner:
                blocking.append(c)
            else:
                ignored_fyi += 1
            continue
        # Own commitment: block if it's something you actually attend (drops
        # placeholders/holds, which is_attendable already excludes).
        if is_attendable(c):
            blocking.append(c)
    return blocking, ignored_fyi


# --- freeform text → a request (or a question) -----------------------------


def known_partner_names(memory: Any) -> list[str]:
    """Best-effort lowercase first names of the user's partner(s) in the household.

    Used only to sharpen partner-detection in the heuristic parse — "dinner with
    Dana" flips ``with_partner`` on when Dana is the co-parent. Every failure mode
    (no household, unscoped store, missing method) degrades to an empty list, so a
    single-user deployment simply relies on the generic partner words.
    """
    try:
        hid = memory.household_id_or_none()
        if hid is None:
            return []
        me = memory.user_id
        kids = {n.strip().lower() for n in memory.child_names()}
        names: list[str] = []
        for m in memory.household_members(hid):
            if m.get("id") == me or m.get("status") not in (None, "active"):
                continue
            label = (m.get("display_name") or m.get("handle") or "").strip()
            first = label.split()[0].lower() if label else ""
            if first and first not in kids:
                names.append(first)
        return names
    except Exception:  # pragma: no cover - defensive; never break scheduling
        return []


def _parse_minutes(message: str) -> float | None:
    """Pull a duration out of free text — "45 min", "an hour", "1.5 hours", "90m"."""
    text = message.lower()
    # Word forms first, so "half an hour" doesn't get read as a bare "hour" = 60.
    if re.search(r"\bhalf an? hour\b|\bhalf-hour\b", text):
        return 30.0
    # "…and a half" riders must beat the plain "an hour" / "N hours" rules below,
    # which would otherwise match first and drop the extra 30 min. Both orderings:
    # "an hour and a half" / "2 hours and a half", and "2 and a half hours".
    m = re.search(r"\b(?:an|(\d+))\s+hours?\s+and a half\b", text)
    if m:
        return (float(m.group(1)) if m.group(1) else 1.0) * 60.0 + 30.0
    m = re.search(r"\b(\d+)\s+and a half\s+hours?\b", text)
    if m:
        return float(m.group(1)) * 60.0 + 30.0
    if re.search(r"\ban hour\b", text):
        return 60.0
    # "1.5 hours", "2 hrs", "2h"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:h\b|hr|hrs|hour|hours)", text)
    if m:
        return float(m.group(1)) * 60.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m\b|min|mins|minute|minutes)", text)
    if m:
        return float(m.group(1))
    return None


def _parse_days(message: str) -> int | None:
    """Map a rough timeframe to a scan horizon in days (from today)."""
    text = message.lower()
    if re.search(r"\btoday\b|\bthis (?:morning|afternoon|evening)\b|\btonight\b", text):
        return 1
    if re.search(r"\btomorrow\b", text):
        return 2
    if re.search(r"\bnext week\b|\bnext (?:mon|tue|wed|thu|fri|sat|sun)", text):
        return MAX_REQUEST_DAYS
    if re.search(r"\bthis week\b|\bthe week\b", text):
        return 7
    return None


def _parse_time_window(message: str) -> tuple[str, str] | None:
    """Map "morning/afternoon/evening/lunch" to a local ``(HH:MM, HH:MM)`` band."""
    text = message.lower()
    for word, band in _TIME_OF_DAY.items():
        if re.search(rf"\b{word}\b", text):
            return band
    return None


def _detect_partner(message: str, partner_names: list[str]) -> bool:
    """Whether the text implies a partner is part of the plan."""
    text = message.lower()
    if re.search(r"\bdate night\b", text):
        return True
    words = set(re.findall(r"[a-z']+", text))
    if words & _PARTNER_WORDS:
        return True
    return any(name in words for name in partner_names)


def _clean_title(message: str) -> str | None:
    """A short label for the ask, or ``None`` when there's nothing worth echoing.

    Only used on the model path as a fallback when the model gave no ``title`` —
    and only for a *short* message, since echoing a long imperative ("find 45
    minutes for a call sometime this week") back as a title reads as noise. The
    heuristic path deliberately passes ``title=None`` rather than the raw ask.
    """
    title = message.strip()
    if not title or len(title) > 40:
        return None
    return title


def _build_request(
    *,
    minutes: float | None,
    days: int | None,
    with_partner: bool,
    title: str | None,
    time_window: tuple[str, str] | None,
    default_days: int,
) -> tuple[ScheduleRequest | None, str | None]:
    """Assemble a validated request, or a clarifying question when duration is unknown.

    Duration is the one field we can't proceed without — everything else has a
    sensible default. So a missing/invalid ``minutes`` yields the question; a
    missing horizon falls back to ``default_days``; an out-of-range value is
    clamped rather than rejected.
    """
    if minutes is None:
        return None, (
            "How long should I set aside? (e.g. 30 minutes, an hour) — "
            "I need a duration to find a slot."
        )
    minutes = max(MIN_REQUEST_MINUTES, min(MAX_REQUEST_MINUTES, float(minutes)))
    horizon = days if days is not None else default_days
    horizon = max(1, min(MAX_REQUEST_DAYS, int(horizon)))
    # A malformed or wrapping (overnight) band is meaningless; drop it and let the
    # full waking band apply, exactly as find_slots would.
    if time_window is not None:
        parsed = parse_window(f"{time_window[0]}-{time_window[1]}")
        if parsed is None or parsed[0] >= parsed[1]:
            time_window = None
    return (
        ScheduleRequest(
            minutes=minutes,
            days=horizon,
            with_partner=with_partner,
            title=title,
            time_window=time_window,
        ),
        None,
    )


def _heuristic_request(
    message: str, *, partner_names: list[str], default_days: int
) -> tuple[ScheduleRequest | None, str | None]:
    """Offline parse of a "find me a time" ask (no model needed)."""
    return _build_request(
        minutes=_parse_minutes(message),
        days=_parse_days(message),
        with_partner=_detect_partner(message, partner_names),
        # No reliable way to pull a clean event label out of a raw imperative
        # offline, so don't echo the whole ask back as a title — the model path
        # names the event; here we leave it unset.
        title=None,
        time_window=_parse_time_window(message),
        default_days=default_days,
    )


#: System prompt for the model interpret path. Keeps the model to *parsing* — the
#: slot-finding itself is deterministic Python, so the model never invents a time.
AVAILABILITY_SYSTEM = (
    "You turn a person's request to find a free time into a small JSON object. "
    "Do NOT pick a time yourself — a separate system finds open slots from the "
    "calendar. Reply with ONLY this JSON object:\n"
    '{"minutes": <int or null>, "days": <int 1-14 or null>, '
    '"with_partner": <true/false>, '
    '"time_of_day": {"start": "HH:MM", "end": "HH:MM"} or null, '
    '"title": <short string or null>, "question": <string or null>}\n'
    "Rules:\n"
    "- minutes: the meeting length. If the person did not say and you cannot "
    "reasonably infer it, set minutes=null AND put a short question asking how "
    "long to set aside.\n"
    "- days: how many days from today to look. today=1, tomorrow=2, this week=7, "
    "next week=14. null if unspecified.\n"
    "- with_partner: true if the plan involves the person's partner/spouse or "
    'both of them ("my wife and I", "date night", "the two of us"); false if it '
    "is only the person themselves.\n"
    "- time_of_day: a clock band only if they gave one (morning=06:00-12:00, "
    "afternoon=12:00-17:00, evening=17:00-22:00); otherwise null.\n"
    "- title: a few words naming the event; null if unclear.\n"
    "- question: set ONLY when you cannot proceed (almost always a missing "
    "duration); otherwise null."
)


def _time_window_from_model(value: Any) -> tuple[str, str] | None:
    """Coerce the model's ``time_of_day`` object into a ``(HH:MM, HH:MM)`` tuple."""
    if not isinstance(value, dict):
        return None
    start, end = value.get("start"), value.get("end")
    if isinstance(start, str) and isinstance(end, str):
        return (start, end)
    return None


def interpret_request(
    message: str,
    *,
    client: Any = None,
    partner_names: list[str] | None = None,
    default_days: int = DEFAULT_SLOT_DAYS,
) -> tuple[ScheduleRequest | None, str | None]:
    """Turn free text into a :class:`ScheduleRequest`, or a clarifying question.

    Tries the model first (``client``); on any failure — unreachable, or a reply
    that doesn't parse — falls back to the offline heuristic
    (:func:`_heuristic_request`), so this degrades to a usable answer with no
    model present. Returns ``(request, None)`` when we can proceed and
    ``(None, question)`` when we need to ask first.

    Args:
        message: The user's free-text ask.
        client: A model client (Ollama/Anthropic) or ``None`` to skip straight to
            the heuristic.
        partner_names: Lowercase partner first names (from
            :func:`known_partner_names`) that sharpen the heuristic's
            partner detection; ignored on the model path.
        default_days: Horizon to use when the text gives no timeframe.
    """
    partner_names = partner_names or []
    parsed = generate_json(message, system=AVAILABILITY_SYSTEM, client=client) if client else None
    if not isinstance(parsed, dict):
        return _heuristic_request(
            message, partner_names=partner_names, default_days=default_days
        )

    def _num(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    minutes = _num(parsed.get("minutes"))
    days_num = _num(parsed.get("days"))
    days = int(days_num) if days_num is not None else None
    question = parsed.get("question")
    question = question.strip() if isinstance(question, str) and question.strip() else None
    title = parsed.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else _clean_title(message)

    # The model must ask when it couldn't find a duration; honor its own question
    # rather than our generic one when it phrased one. (Who's-involved that we did
    # infer isn't lost — plan_availability still echoes it on the question path.)
    if minutes is None:
        _, fallback_q = _build_request(
            minutes=None, days=days, with_partner=False, title=title,
            time_window=None, default_days=default_days,
        )
        return None, (question or fallback_q)

    return _build_request(
        minutes=minutes,
        days=days,
        with_partner=bool(parsed.get("with_partner")),
        title=title,
        time_window=_time_window_from_model(parsed.get("time_of_day")),
        default_days=default_days,
    )


# --- request → open slots --------------------------------------------------


def find_availability(
    commitments: list[dict[str, Any]],
    request: ScheduleRequest,
    *,
    now: datetime,
    tz: str,
    awake_band: tuple[str, str] = ("06:00", "22:00"),
    limit: int = DEFAULT_SLOT_LIMIT,
) -> tuple[list[FreeWindow], int, int]:
    """Open windows that fit ``request``, given who's involved.

    Filters ``commitments`` to just the entries that block for this request's
    participants (:func:`constraint_commitments`), then subtracts them across the
    coming days' waking band with :func:`prefrontal.scheduling.find_slots`. The
    request's ``time_window`` narrows the daily band when set.

    Returns ``(slots, considered, ignored_fyi)`` — the open windows plus how many
    commitments blocked and how many FYI events were ignored (see
    :func:`constraint_commitments`).
    """
    blocking, ignored_fyi = constraint_commitments(
        commitments, with_partner=request.with_partner
    )
    band = request.time_window or awake_band
    slots = find_slots(
        blocking,
        now,
        tz,
        minutes=request.minutes,
        days=request.days,
        awake_band=band,
        limit=limit,
    )
    return slots, len(blocking), ignored_fyi


def plan_availability(
    message: str,
    memory: Any,
    *,
    client: Any = None,
    now: datetime | None = None,
    tz: str = "UTC",
    awake_band: tuple[str, str] = ("06:00", "22:00"),
    default_days: int = DEFAULT_SLOT_DAYS,
) -> AvailabilityPlan:
    """End-to-end: free text + a scoped store → a question or a list of open slots.

    Reads the user's upcoming commitments, interprets the ask
    (:func:`interpret_request`), and — when the ask is answerable — returns the
    fitting slots scoped to who's involved. When it isn't (no duration), returns
    the clarifying question instead. Never writes.
    """
    now = now or utcnow()
    partner_names = known_partner_names(memory)
    request, question = interpret_request(
        message, client=client, partner_names=partner_names, default_days=default_days
    )
    if request is None:
        # Even while clarifying, echo who we already understood to be involved, so
        # the client doesn't have to re-ask "is this for both of you?" too.
        return AvailabilityPlan(
            question=question,
            with_partner=_detect_partner(message, partner_names),
        )

    # Window the commitments by the *same* ``now`` we slot against, so the blocking
    # set and the free windows agree — otherwise the store filters "upcoming" by
    # the wall clock while find_slots reasons from an injected ``now``, and the two
    # disagree (e.g. a test's fixed clock, or a request planned as-of a past instant).
    slots, considered, ignored_fyi = find_availability(
        memory.upcoming_commitments(limit=1000, now=now),
        request,
        now=now,
        tz=tz,
        awake_band=awake_band,
    )
    return AvailabilityPlan(
        question=None,
        request=request,
        slots=slots,
        with_partner=request.with_partner,
        considered=considered,
        ignored_fyi=ignored_fyi,
    )


# --- rendering (for the CLI and plain-text channels) -----------------------


def _slot_line(w: FreeWindow, tz: str) -> str:
    """A one-line local rendering of a free window: "Tue Jul 14, 2:00–4:30 PM"."""
    start = local_datetime(parse_ts(w.start) or utcnow(), tz)
    end = local_datetime(parse_ts(w.end) or utcnow(), tz)
    return (
        f"{start:%a %b %-d}, {start:%-I:%M}–{end:%-I:%M %p}"
        f" ({int(w.minutes)} min free)"
    )


def render_plan(plan: AvailabilityPlan, tz: str = "UTC", *, max_slots: int = 6) -> str:
    """Render an :class:`AvailabilityPlan` as plain text for a CLI / n8n channel."""
    if plan.question is not None:
        return plan.question
    request = plan.request
    assert request is not None  # question is None ⇒ request is set

    who = "you and your partner" if plan.with_partner else "you"
    label = f" for “{request.title}”" if request.title else ""
    header = f"Open {int(request.minutes)}-min slots{label} ({who} free):"

    if not plan.slots:
        note = (
            " Try a wider timeframe, a shorter block, or a different time of day."
        )
        return (
            f"No {int(request.minutes)}-min slot fits {who} in the next "
            f"{request.days} day{'s' if request.days != 1 else ''}." + note
        )

    lines = [header]
    lines.extend(f"  • {_slot_line(w, tz)}" for w in plan.slots[:max_slots])
    if len(plan.slots) > max_slots:
        lines.append(f"  …and {len(plan.slots) - max_slots} more.")
    if plan.ignored_fyi and not plan.with_partner:
        n = plan.ignored_fyi
        item, verb = ("items", "are") if n != 1 else ("item", "is")
        lines.append(
            f"(Ignored {n} FYI {item} that {verb} someone else's — "
            "say it's for both of you to include them.)"
        )
    return "\n".join(lines)
