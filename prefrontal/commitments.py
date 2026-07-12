"""Commitments ingestion — the schedule layer.

Prefrontal reasons about *upcoming commitments* so it can eventually answer
"what gets thrown off when I'm running behind" (impact analysis). Commitments are
populated two ways:

- **Calendar sync** — an n8n workflow pulls events from Google Calendar (or
  CalDAV), maps them to the commitment shape, and POSTs them to
  ``/webhooks/calendar/sync``, which calls :func:`sync_calendar` here.
- **Manual** — a single commitment added via ``POST /commitments``.

This module owns the domain logic: normalizing timestamps to UTC and the
idempotent sync (upsert provided events, prune calendar events that disappeared).
Persistence lives in :class:`~prefrontal.memory.store.MemoryStore`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.parser import isoparse
from dateutil.rrule import rrulestr

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.clock import parse_ts_strict as _parse_utc
from prefrontal.focus_balance import normalize_focus_domain
from prefrontal.log import get_logger
from prefrontal.memory.store import MemoryStore

logger = get_logger(__name__)

#: Assumed duration for a commitment with no ``end_at`` (overlap detection).
DEFAULT_EVENT_MINUTES = 30.0

#: How far ahead recurring events are expanded into concrete occurrences, by
#: default — 30 days, so the calendar surfaces (the calendar page, slot finder)
#: see standing/weekly events a month out, not just the next day. One-off events
#: have no horizon (they ingest whenever they land); this only bounds how far a
#: recurring *series* is materialized (a series must be bounded — a never-ending
#: RRULE would otherwise expand forever). Deployments tune it via
#: ``PREFRONTAL_CALENDAR_HORIZON_DAYS`` → :attr:`Settings.calendar_horizon_days`,
#: which the CLI/webhook sync pass through as ``recur_horizon_hours``. The stable
#: per-occurrence ``external_id`` means a wider window just upserts more rows, never
#: duplicates, and each poll rolls the window forward.
RECUR_HORIZON_HOURS = 24.0 * 30

#: Hard ceiling on occurrences materialized from a single RRULE master, regardless
#: of horizon. The horizon already bounds a *sane* series (daily-over-a-month = ~30,
#: hourly = ~720), but a hostile or fat-fingered ``FREQ=MINUTELY``/``SECONDLY`` feed
#: would otherwise expand to tens of thousands / millions of dicts in one sync — an
#: unbounded-memory DoS. This caps that at a value comfortably above any real
#: calendar cadence; hitting it is logged so a legitimate high-frequency series that
#: got truncated is visible rather than silent.
MAX_RECURRENCE_OCCURRENCES = 2000

#: Joins a recurring series' id to a generated occurrence's start stamp, forming a
#: stable per-occurrence ``external_id`` so repeated polls upsert (never duplicate).
RECUR_OCCURRENCE_SEP = "::"

#: Windows/Outlook timezone names → IANA zones. Outlook-published ICS feeds label
#: ``TZID`` with Windows names (``Eastern Standard Time``) that :mod:`zoneinfo`
#: can't load directly, so we translate the common ones. A ``TZID`` not covered
#: here is tried verbatim as an IANA name, then falls back to the deployment's
#: home timezone — so an unmapped zone degrades to "home" rather than silently
#: landing hours off in UTC. (Subset of the Unicode CLDR windowsZones mapping.)
_WINDOWS_TZ = {
    "Dateline Standard Time": "Etc/GMT+12",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time": "America/Denver",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "Eastern Standard Time": "America/New_York",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "SA Pacific Standard Time": "America/Bogota",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "GTB Standard Time": "Europe/Bucharest",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arabic Standard Time": "Asia/Baghdad",
    "Arab Standard Time": "Asia/Riyadh",
    "Russian Standard Time": "Europe/Moscow",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC": "UTC",
}


def _zone(name: str | None) -> ZoneInfo | None:
    """Load an IANA (or Windows-mapped) zone name, or ``None`` if unresolvable."""
    if not name:
        return None
    name = name.strip()
    for candidate in (name, _WINDOWS_TZ.get(name)):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def resolve_zone(tzid: str | None, default_tz: str) -> ZoneInfo:
    """Resolve a ``TZID`` to a concrete zone, falling back to the home timezone.

    Tries the ``tzid`` as an IANA name, then via the Windows→IANA map, then the
    ``default_tz`` (the deployment's home zone), and finally UTC — so an unknown
    or absent zone degrades predictably instead of raising.
    """
    return _zone(tzid) or _zone(default_tz) or ZoneInfo("UTC")

#: Commitment ``kind`` — whether an event is *yours* (something you attend, can
#: conflict) or *FYI*: just so you know where someone will be (e.g. a partner's
#: "Harlequin Brow Appt"). FYI commitments are shown but never treated as a
#: double-booking. See :mod:`prefrontal.classify`.
KIND_SELF = "self"
KIND_FYI = "fyi"
#: A kid's appointment (dentist, doctor, school event). Like ``self`` it's a real
#: obligation someone must cover, but it belongs to the shared household sheet
#: (docs/household-sheet.md §3.7) — the sheet surfaces the upcoming ones so both
#: co-parents see them and "who's on pickup" is legible.
KIND_CHILD = "child"
KINDS = (KIND_SELF, KIND_FYI, KIND_CHILD)

#: Commitment ``outcome`` — the user's honest self-report on a *past* commitment,
#: set from the "I made it / missed it" affordance the dashboard shows on recently
#: elapsed commitments. ``None`` means undecided (still awaiting an answer). Like
#: ``hidden``, it's a user judgement that survives calendar re-sync.
OUTCOME_MADE = "made"
OUTCOME_MISSED = "missed"
OUTCOMES = (OUTCOME_MADE, OUTCOME_MISSED)

#: Commitment ``hardness`` — is this a firm, must-happen obligation (``hard``) or an
#: elastic/optional block you'd yield in a pinch (``soft``, the default)? It gates
#: whether panic/cascade treats a slipping commitment as a genuine fire (see
#: :mod:`prefrontal.panic`). ``hardness_source`` mirrors ``kind_source``: a value
#: set by the user (``set_commitment_hardness``) is preserved across calendar
#: re-syncs, while a ``feed``/``default`` one is refreshed from the calendar.
HARDNESS_HARD = "hard"
HARDNESS_SOFT = "soft"
HARDNESS = (HARDNESS_HARD, HARDNESS_SOFT)

#: Placeholder/hold titles that aren't real events. When one of these overlaps a
#: specifically-titled event (e.g. a work "Block" mirroring a real personal
#: meeting), it's a *possible* conflict — surfaced softly and dismissable —
#: rather than a firm double-booking. Matched against a normalized title.
GENERIC_TITLES = frozenset({
    "busy", "block", "blocked", "hold", "hold time", "held", "ooo",
    "out of office", "out", "tentative", "private", "focus", "focus time",
    "reserved", "placeholder", "do not schedule", "dns", "no meetings",
    "no meeting", "tbd",
})


def _normalize_title(title: str | None) -> str:
    """Lowercase, strip punctuation/emoji, collapse spaces — for placeholder match."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def is_placeholder_title(title: str | None) -> bool:
    """Whether a title is a generic placeholder/hold rather than a real event."""
    return _normalize_title(title) in GENERIC_TITLES


def is_attendable(commitment: dict[str, Any]) -> bool:
    """Whether a commitment is a real event *you* personally attend at a fixed time.

    The two surfaces that model your time as a sequence of fixed blocks — the
    running-behind cascade (:func:`prefrontal.impact.cascade_impact`) and the
    departure reminder (:func:`prefrontal.departure.plan_upcoming_departures`) —
    both want the same subset: events that are actually yours to show up to. Two
    kinds of calendar entry are *not* that, and must be excluded from both:

    - **FYI** events (``kind == "fyi"``) — where someone *else* will be (a
      partner's appointment, a kid's lesson). They're never yours to attend, so
      they can happen simultaneously with your own commitments: they must not
      consume your time, topple anything in the cascade, or fire a departure
      nudge (you're not going anywhere for them).
    - **Placeholder/hold** blocks ("HOLD", "OOO", "Focus", "Block", …; see
      :data:`GENERIC_TITLES`) — elastic time you'd yield the instant a real
      commitment needed it. A domino that only pushes a hold isn't something
      you're "behind" on, and there's nothing to "leave by" for a hold. A hold
      overlapping a real meeting is a soft possible-conflict
      (:func:`is_possible_conflict`), not a serial predecessor or a nudge.

    Anything else is a real, own commitment: it consumes time, can be toppled,
    and is worth a leave-by reminder.
    """
    if commitment.get("kind") == "fyi":
        return False
    return not is_placeholder_title(commitment.get("title"))


def is_possible_conflict(conflict: Conflict) -> bool:
    """A *possible* (soft) conflict — at least one side is a placeholder title."""
    return is_placeholder_title(conflict.a.get("title")) or is_placeholder_title(
        conflict.b.get("title")
    )


def conflict_dismissal_key(conflict: Conflict) -> str:
    """A stable key for dismissing a conflict pair (possible or hard double-booking).

    Keyed by the two events' **identities** alone (``external_id``, or the row id
    for a manual commitment) — not their start times or titles — so a dismissal
    stays put by *pair*: once you've said "I know these two overlap and that's
    fine", it stays dismissed even if one event is rescheduled or retitled, as long
    as both still exist and still overlap (a non-overlap produces no conflict to
    key at all, and a fresh overlap between a *different* pair has a different key).
    """

    def ident(c: dict[str, Any]) -> str:
        return c.get("external_id") or f"id:{c.get('id')}"

    return "::".join(sorted([ident(conflict.a), ident(conflict.b)]))


def partition_conflicts(
    conflicts: list[Conflict], dismissed: set[str]
) -> tuple[list[Conflict], list[Conflict]]:
    """Split conflicts into ``(hard, possible)``, dropping any the user dismissed.

    *Hard* = a real double-booking (both sides specifically titled). *Possible* =
    one side is a placeholder. A pair whose :func:`conflict_dismissal_key` is in
    ``dismissed`` is dropped from **either** category — so a dismissed
    double-booking stops firing the sync alert and stops showing in the conflicts
    list, exactly as a dismissed possible one does.
    """
    hard: list[Conflict] = []
    possible: list[Conflict] = []
    for c in conflicts:
        if conflict_dismissal_key(c) in dismissed:
            continue  # user waved this pair off (hard or possible) — don't surface it
        (possible if is_possible_conflict(c) else hard).append(c)
    return hard, possible


def undismissed_conflicts(
    conflicts: list[Conflict], dismissed: set[str]
) -> list[Conflict]:
    """The conflicts the user hasn't dismissed — a flat filter (hard + possible).

    The partition-free companion to :func:`partition_conflicts` for surfaces that
    treat every overlap the same (the briefing's double-booking line, the
    encouragement "rough day" score): keep a conflict unless its
    :func:`conflict_dismissal_key` is in ``dismissed``, so a dismissal is honored
    everywhere a conflict is surfaced, not just in the conflicts endpoint.
    """
    return [c for c in conflicts if conflict_dismissal_key(c) not in dismissed]


@dataclass(frozen=True)
class SyncSummary:
    """Result of a :func:`sync_calendar` run."""

    added: int
    updated: int
    cancelled: int
    upcoming: int
    conflicts: int
    new_conflict: bool
    possible_conflicts: int
    new_possible_conflict: bool
    #: Events dropped because they failed validation (a malformed VEVENT no longer
    #: rejects the whole feed — see :func:`sync_calendar`). ``skipped`` is the
    #: count; ``skipped_titles`` names them (for the CLI/webhook to surface).
    skipped: int = 0
    skipped_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conflict:
    """Two overlapping commitments — a double-booking."""

    a: dict[str, Any]
    b: dict[str, Any]
    overlap_minutes: float


def to_utc(value: str, tzid: str | None = None, default_tz: str = "UTC") -> str:
    """Normalize an ISO-8601 timestamp to ``YYYY-MM-DD HH:MM:SS`` in UTC.

    The output format matches SQLite's ``datetime('now')`` so string comparison
    Just Works. How the input's zone is determined, in priority order:

    - **Offset-aware** (``2026-06-28T10:30:00-07:00``) or **Z**-suffixed: the
      embedded offset is authoritative and ``tzid`` is ignored.
    - **Naive with a** ``tzid``: interpreted in that zone (IANA or a Windows name
      like ``Eastern Standard Time``) — this is the ICS ``DTSTART;TZID=…`` case.
    - **Naive without a** ``tzid``: interpreted in ``default_tz`` (the
      deployment's home timezone). A floating ICS time or a manual entry.
    - **Date-only** (``2026-06-28``): treated as floating midnight and left as
      ``… 00:00:00`` — all-day events aren't shifted across a day boundary.

    An unresolvable ``tzid`` falls back to ``default_tz`` (then UTC), so a zone
    we don't recognize degrades to "home" rather than silently landing hours off.

    Args:
        value: An ISO-8601 date or datetime string.
        tzid: Optional source zone for a naive ``value`` (IANA or Windows name).
        default_tz: Home zone for a naive ``value`` with no usable ``tzid``.

    Returns:
        A normalized UTC timestamp string.

    Raises:
        ValueError: If ``value`` is not a parseable ISO-8601 timestamp.
    """
    text = value.strip()
    # dateutil.isoparse (not datetime.fromisoformat) so the parse is uniform
    # across Python versions: 3.10's fromisoformat rejects both a 'Z' suffix and
    # the separator-less basic form (2026…Z / 20260628T073000Z) that ICS feeds
    # emit; isoparse accepts them on every version.
    dt = isoparse(text)
    if dt.tzinfo is not None:
        # An explicit offset wins outright; any tzid is redundant/ignored.
        return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime(TS_FMT)
    if len(text) <= 10:
        # Date-only (all-day): floating midnight, never shifted across a day.
        return dt.strftime(TS_FMT)
    # Naive datetime: attach the source zone and convert to UTC.
    localized = dt.replace(tzinfo=resolve_zone(tzid, default_tz))
    return localized.astimezone(timezone.utc).replace(tzinfo=None).strftime(TS_FMT)


def normalize_event(
    event: dict[str, Any], *, default_tz: str = "UTC"
) -> dict[str, Any]:
    """Validate and normalize one inbound calendar/commitment event.

    Args:
        event: A raw event dict. Requires ``title`` and ``start_at``; optional
            ``external_id``, ``end_at``, ``location``, ``notes`` (free-text
            detail consulted when a nudge is built for the commitment), ``url``
            (a.k.a. ``source_url`` / ``html_link``, a deeplink to the source
            event), ``dest_lat``, ``dest_lon``, ``lead_minutes``, ``hard``, and
            ``tzid`` / ``end_tzid`` (source zones for naive ``start_at`` /
            ``end_at``, as sent by the ICS parser). ``end_tzid`` defaults to
            ``tzid`` when absent.
        default_tz: Home timezone for naive timestamps with no ``tzid``.

    Returns:
        Kwargs ready for :meth:`MemoryStore.upsert_commitment`.

    Raises:
        ValueError: If a required field is missing or a timestamp is unparseable.
    """
    title = (event.get("title") or "").strip()
    if not title:
        raise ValueError("commitment is missing 'title'")
    if not event.get("start_at"):
        raise ValueError("commitment is missing 'start_at'")

    end_raw = event.get("end_at")
    lead = event.get("lead_minutes")
    dest_lat = event.get("dest_lat")
    dest_lon = event.get("dest_lon")
    tzid = event.get("tzid")
    end_tzid = event.get("end_tzid") or tzid
    raw_source = event.get("source") or "calendar"
    is_hard = bool(event.get("hard"))
    # Provenance of the hardness value (mirrors kind_source). Only a `user` value
    # is sticky across re-sync; a manual hard commitment is a user assertion, a
    # calendar hard flag is `feed`, and a plain soft default is `default`.
    if not is_hard:
        hardness_source = "default"
    elif raw_source == "manual":
        hardness_source = "user"
    else:
        hardness_source = "feed"
    return {
        "title": title,
        "start_at": to_utc(event["start_at"], tzid=tzid, default_tz=default_tz),
        "external_id": event.get("external_id") or None,
        "end_at": (
            to_utc(end_raw, tzid=end_tzid, default_tz=default_tz) if end_raw else None
        ),
        "location": event.get("location") or None,
        "notes": (event.get("notes") or "").strip() or None,
        "source_url": (
            event.get("url") or event.get("source_url") or event.get("html_link")
        )
        or None,
        "dest_lat": float(dest_lat) if dest_lat is not None else None,
        "dest_lon": float(dest_lon) if dest_lon is not None else None,
        "lead_minutes": float(lead) if lead is not None else 10.0,
        "hardness": HARDNESS_HARD if is_hard else HARDNESS_SOFT,
        "hardness_source": hardness_source,
        "source": raw_source,
        # Life-sphere, snapped onto the canonical vocab (child/family → kids, …).
        # Usually absent on a raw calendar event (→ None); supplied by a manual add
        # or a domain-aware feed. A user's later edit survives re-sync (INSERT-only).
        "domain": normalize_focus_domain(event.get("domain")),
    }


def _dedupe_key(fields: dict[str, Any]) -> tuple[str, str, str]:
    """Identity of a *real* event for cross-calendar dedup: title + start + end.

    Two events collapse when their normalized titles and UTC start/end match, so a
    meeting mirrored onto two of your feeds is recognized as one regardless of the
    feed-namespaced ``external_id``\\ s that make it look like two. The title is
    normalized (case/punctuation/emoji-insensitive, see :func:`_normalize_title`)
    and a missing ``end_at`` keys as ``""`` so both copies of an all-day/no-end
    event still group.
    """
    return (
        _normalize_title(fields.get("title")),
        fields.get("start_at") or "",
        fields.get("end_at") or "",
    )


def dedupe_events(normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same real event synced from more than one calendar feed.

    A meeting that lands on two of a user's calendars — an invite mirrored onto an
    Outlook feed *and* a work feed — arrives as two events with distinct,
    feed-namespaced ``external_id``\\ s (``outlook:UID`` vs ``work:UID``). Without
    this both are stored, so the event shows twice and, worse, the pair reads as a
    hard double-booking of you against yourself (:func:`find_conflicts`).

    Events sharing a :func:`_dedupe_key` (normalized title + UTC start + end) are
    treated as one. The survivor is chosen deterministically — the lexicographically
    smallest ``external_id`` — so repeated polls keep the *same* stored row rather
    than flip-flopping between feeds (which would churn a cancel/re-add every sync).
    An event with no ``external_id`` sorts last, so a real feed id is preferred as
    the keeper. Input order of the survivors is preserved.

    Args:
        normalized: Events already through :func:`normalize_event` (UTC times), so
            the key compares like-for-like.

    Returns:
        One event per distinct real event, in first-seen order.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, str]] = []
    for fields in normalized:
        key = _dedupe_key(fields)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(fields)
    out: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        # U+FFFF sorts after any real id, so a null-id event loses the keeper
        # tie-break to any event that carries a (feed) external_id.
        out.append(min(group, key=lambda f: f.get("external_id") or "￿"))
    return out


def _bare_uid(external_id: str) -> str:
    """Strip the feed namespace (``work:``/``personal:``/…) from an event id.

    Occurrence overrides are matched on the bare UID so a modified instance on
    one feed suppresses the generated occurrence even if its master was deduped
    in from a different feed.
    """
    return re.sub(r"^[^:]+:", "", external_id)


def _aware(value: str, tzid: str | None, default_tz: str) -> datetime | None:
    """Parse an ICS timestamp to a timezone-aware :class:`datetime`, or ``None``.

    Offset-aware / ``Z`` values keep their own offset; a naive value is localized
    to ``tzid`` (or ``default_tz``). Unlike :func:`to_utc` this returns the aware
    object itself, so recurrence math happens in the event's own wall clock —
    keeping a 7:30 local event at 7:30 across DST rather than drifting an hour.
    """
    try:
        # isoparse (not fromisoformat) to accept 'Z' and separator-less basic ICS
        # timestamps on Python 3.10 too (see to_utc).
        dt = isoparse(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=resolve_zone(tzid, default_tz))


def expand_recurrences(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    horizon_hours: float = RECUR_HORIZON_HOURS,
    back_hours: float = 1.0,
    default_tz: str = "UTC",
) -> list[dict[str, Any]]:
    """Expand ``RRULE`` master events into concrete near-term occurrences.

    ICS feeds export an *unmodified* recurring event as a single master VEVENT —
    its original ``DTSTART`` plus an ``RRULE`` — never a copy per week. So a
    long-standing weekly event's start sits months in the past and is dropped by
    any forward window, and it silently never appears. This turns each master
    into one event per occurrence within ``[now - back_hours, now + horizon]``,
    honoring ``EXDATE`` skips and suppressing an occurrence that a modified
    instance (``RECURRENCE-ID``) already stands in for. One-off events and the
    modified instances themselves pass through untouched.

    Each generated occurrence gets a stable ``external_id`` of
    ``<master id>::<YYYYMMDDTHHMMSS>``, so repeated polls upsert rather than
    duplicate it. A master whose ``RRULE`` can't be parsed is skipped, never
    rejecting the whole batch.

    Args:
        events: Raw event dicts (as the ICS parser emits them), optionally
            carrying ``rrule``, ``exdate`` (list), and ``recurrence_id``.
        now: The reference instant (timezone-aware) the window is measured from.
        horizon_hours: How far ahead to generate occurrences.
        back_hours: How far back to include a just-started occurrence.
        default_tz: Home zone for naive timestamps with no ``tzid``.

    Returns:
        A new event list with masters replaced by their occurrences.
    """
    # Occurrences generated below are timezone-aware (dtstart is localized via
    # `_aware`), so the window bounds must be aware too — dateutil raises a
    # TypeError comparing aware occurrences against a naive bound, which the
    # `except` in `_expand_master` would swallow, silently dropping every
    # recurring event. Production passes a naive UTC `now` (clock.utcnow()), so
    # normalize it here rather than trusting every caller to pre-attach a zone.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_start = now - timedelta(hours=back_hours)
    window_end = now + timedelta(hours=horizon_hours)

    # Original start instants a modified instance already covers, keyed by bare
    # UID. dateutil generates occurrences at those original times, so we skip
    # them and let the moved instance's own (concrete) VEVENT stand in.
    overrides: dict[str, set[datetime]] = {}
    for e in events:
        rid = e.get("recurrence_id")
        if not rid:
            continue
        dt = _aware(rid, e.get("tzid"), default_tz)
        if dt is not None:
            key = _bare_uid(e.get("external_id") or "")
            overrides.setdefault(key, set()).add(dt.astimezone(timezone.utc))

    out: list[dict[str, Any]] = []
    for e in events:
        rule_text = e.get("rrule")
        if not rule_text:
            out.append(e)  # one-off, or an already-concrete modified instance
            continue
        out.extend(
            _expand_master(e, rule_text, window_start, window_end, overrides, default_tz)
        )
    return out


def _expand_master(
    master: dict[str, Any],
    rule_text: str,
    window_start: datetime,
    window_end: datetime,
    overrides: dict[str, set[datetime]],
    default_tz: str,
) -> list[dict[str, Any]]:
    """Generate the in-window occurrences of one ``RRULE`` master event."""
    tzid = master.get("tzid")
    dtstart = _aware(master.get("start_at") or "", tzid, default_tz)
    if dtstart is None:
        return []
    try:
        rule = rrulestr(rule_text.replace("RRULE:", "", 1), dtstart=dtstart)
        # Iterate lazily (xafter) rather than materializing rule.between(...): a
        # high-frequency RRULE over the horizon can yield millions of occurrences,
        # and list(between) builds them all before we can bound them. Walk in order,
        # stop at window_end, and hard-cap the count so a hostile feed can't exhaust
        # memory (see MAX_RECURRENCE_OCCURRENCES).
        occurrences: list[datetime] = []
        for occ in rule.xafter(window_start, inc=True):
            if occ > window_end:
                break
            occurrences.append(occ)
            if len(occurrences) >= MAX_RECURRENCE_OCCURRENCES:
                logger.warning(
                    "RRULE for %r hit the %d-occurrence cap within the horizon; "
                    "truncating (check the feed's frequency)",
                    master.get("external_id") or master.get("title") or "?",
                    MAX_RECURRENCE_OCCURRENCES,
                )
                break
    except (ValueError, TypeError):
        return []  # a malformed RRULE must never sink the whole sync

    duration: timedelta | None = None
    end0 = _aware(master.get("end_at") or "", master.get("end_tzid") or tzid, default_tz)
    if end0 is not None and end0 > dtstart:
        duration = end0 - dtstart

    skip = set(overrides.get(_bare_uid(master.get("external_id") or ""), set()))
    for raw in master.get("exdate") or []:
        for part in str(raw).split(","):
            d = _aware(part, tzid, default_tz)
            if d is not None:
                skip.add(d.astimezone(timezone.utc))

    base_id = master.get("external_id")
    results: list[dict[str, Any]] = []
    for occ in occurrences:
        if occ.astimezone(timezone.utc) in skip:
            continue
        ev = {
            k: v
            for k, v in master.items()
            if k not in ("rrule", "exdate", "recurrence_id")
        }
        # The occurrence is offset-aware, so to_utc reads its offset directly and
        # the (now-redundant) master tzid is cleared.
        ev["start_at"] = occ.isoformat()
        ev["tzid"] = None
        if duration is not None:
            ev["end_at"] = (occ + duration).isoformat()
            ev["end_tzid"] = None
        else:
            ev["end_at"] = None
        if base_id:
            # tz-ok: iCal occurrence UID, intentional UTC wire format (not display)
            stamp = occ.strftime("%Y%m%dT%H%M%S")
            ev["external_id"] = f"{base_id}{RECUR_OCCURRENCE_SEP}{stamp}"
        results.append(ev)
    return results


def sync_calendar(
    store: MemoryStore,
    events: list[dict[str, Any]],
    *,
    classify: Callable[[str], tuple[str, str]] | None = None,
    default_tz: str = "UTC",
    now: datetime | None = None,
    recur_horizon_hours: float = RECUR_HORIZON_HOURS,
) -> SyncSummary:
    """Idempotently sync a batch of calendar events into ``commitments``.

    Upserts every provided event (by ``external_id``) and cancels future
    calendar commitments that are no longer present — so the stored schedule
    mirrors the calendar window. Manual commitments are untouched.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        events: Raw event dicts (see :func:`normalize_event`).
        classify: Optional ``title -> (kind, kind_source)`` callable used to tag
            *new* events as ``self``/``fyi`` (see :mod:`prefrontal.classify`).
            Events already in the store keep their existing ``kind`` — so a
            recurring event isn't re-classified every poll and a user's manual
            correction is never overwritten. ``None`` leaves new events ``self``.
        default_tz: Home timezone for interpreting naive event times that arrive
            without their own ``tzid`` (see :func:`to_utc`).
        now: Reference instant for expanding recurring events (defaults to the
            current UTC time); injectable for tests.
        recur_horizon_hours: How far ahead to materialize recurring occurrences
            (default :data:`RECUR_HORIZON_HOURS`, 30 days). One-off events are
            unaffected — they carry no horizon.

    Returns:
        A :class:`SyncSummary`. An event that fails validation (missing title,
        unparseable time) is **skipped**, not fatal — the good events still sync
        and ``SyncSummary.skipped`` / ``skipped_titles`` report what was dropped,
        so one malformed VEVENT can't silently kill a whole feed. (A document-level
        parse failure is caught upstream in :func:`prefrontal.ics.parse_ics`.)
    """
    # Turn recurring masters into concrete near-term occurrences before anything
    # else — a weekly event ships as one long-past-dated master, so this is what
    # makes today's instance exist at all.
    events = expand_recurrences(
        events, now=now or utcnow(), default_tz=default_tz,
        horizon_hours=recur_horizon_hours,
    )
    # Validate each event independently: a single malformed VEVENT (missing
    # title, unparseable time) is skipped and logged rather than rejecting the
    # whole feed — one bad event must not stop every other event from syncing.
    # (A document-level parse failure is caught earlier, in parse_ics.)
    normalized = []
    skipped_titles: list[str] = []
    for e in events:
        try:
            normalized.append(normalize_event(e, default_tz=default_tz))
        except ValueError as exc:
            label = str(e.get("title") or e.get("external_id") or "untitled event")
            skipped_titles.append(label)
            logger.warning("sync_calendar skipped an invalid event (%s): %s", label, exc)
    # Collapse the same real event mirrored across feeds (outlook + work) so it's
    # stored once, shown once, and never conflicts with itself.
    normalized = dedupe_events(normalized)
    eids = {f["external_id"] for f in normalized if f["external_id"]}
    existing_kinds = store.kinds_by_external_id(eids)
    existing_hardness = store.hardness_by_external_id(eids)
    added = updated = 0
    keep: set[str] = set()
    for fields in normalized:
        eid = fields["external_id"]
        if eid and eid in existing_kinds:
            kind, kind_source = existing_kinds[eid]  # keep prior verdict
        elif classify is not None:
            kind, kind_source = classify(fields["title"])
        else:
            kind, kind_source = KIND_SELF, "default"
        # A user's hardness override is sticky — reuse it so the feed can't reset
        # a commitment you deliberately marked hard/soft (mirrors the kind reuse).
        # A feed/default value stays feed-driven, refreshed from this event.
        if eid and existing_hardness.get(eid, (None, None))[1] == "user":
            fields["hardness"], fields["hardness_source"] = existing_hardness[eid]
        _, created = store.upsert_commitment(
            **fields, kind=kind, kind_source=kind_source
        )
        if eid:
            keep.add(eid)
        if created:
            added += 1
        else:
            updated += 1
    cancelled = store.cancel_missing_calendar(keep)
    upcoming = store.upcoming_commitments()
    # Split overlaps into firm double-bookings vs. soft "possible" ones (a
    # placeholder Busy/Block overlapping a real event); drop dismissed possibles.
    hard, possible = partition_conflicts(
        find_conflicts(upcoming), store.dismissed_conflicts()
    )

    # Only flag a *new* situation, so a standing conflict doesn't re-alert every
    # poll: remember the last signature and report "new" only when it changes to
    # a non-empty set. Hard and possible are tracked independently.
    hard_sig = _conflict_signature(hard)
    new_conflict = bool(hard) and hard_sig != store.get_state(
        "last_conflict_signature", ""
    )
    store.set_state("last_conflict_signature", hard_sig, source="inferred")

    possible_sig = ";".join(sorted(conflict_dismissal_key(c) for c in possible))
    new_possible_conflict = bool(possible) and possible_sig != store.get_state(
        "last_possible_signature", ""
    )
    store.set_state("last_possible_signature", possible_sig, source="inferred")

    return SyncSummary(
        added=added,
        updated=updated,
        cancelled=cancelled,
        upcoming=len(upcoming),
        conflicts=len(hard),
        new_conflict=new_conflict,
        possible_conflicts=len(possible),
        new_possible_conflict=new_possible_conflict,
        skipped=len(skipped_titles),
        skipped_titles=tuple(skipped_titles),
    )


def _conflict_signature(conflicts: list[Conflict]) -> str:
    """A stable string identifying a set of conflicts (order-independent).

    Keys each pair by ``external_id`` (falling back to row id), sorts within and
    across pairs, so the same double-bookings always produce the same signature.
    """
    pairs = []
    for c in conflicts:
        a = c.a.get("external_id") or f"id:{c.a.get('id')}"
        b = c.b.get("external_id") or f"id:{c.b.get('id')}"
        pairs.append("|".join(sorted([str(a), str(b)])))
    return ";".join(sorted(pairs))


def _interval(commitment: dict[str, Any], default_minutes: float) -> tuple[datetime, datetime]:
    """Return the ``(start, end)`` interval for a commitment.

    ``end`` is the stored ``end_at`` when present and after the start, otherwise
    ``start + default_minutes``.
    """
    start = _parse_utc(commitment["start_at"])
    end_raw = commitment.get("end_at")
    if end_raw:
        end = _parse_utc(end_raw)
        if end <= start:
            end = start + timedelta(minutes=default_minutes)
    else:
        end = start + timedelta(minutes=default_minutes)
    return start, end


def find_conflicts(
    commitments: list[dict[str, Any]], default_minutes: float = DEFAULT_EVENT_MINUTES
) -> list[Conflict]:
    """Find double-bookings: pairs of commitments whose times overlap.

    Two commitments conflict when their ``[start, end)`` intervals intersect.
    Commitments without an ``end_at`` are assumed ``default_minutes`` long. This
    is most useful across merged calendars (personal + work), where an overlap is
    invisible to either calendar alone.

    Args:
        commitments: Commitment dicts (typically the upcoming set).
        default_minutes: Assumed duration when ``end_at`` is absent.

    Returns:
        A list of :class:`Conflict`, each with the overlap in minutes.
    """
    # FYI commitments ("where someone will be") never count as double-bookings.
    real = [c for c in commitments if c.get("kind") != KIND_FYI]
    items = sorted(real, key=lambda c: c["start_at"])
    intervals = [(c, *_interval(c, default_minutes)) for c in items]
    conflicts: list[Conflict] = []
    for i, (ci, si, ei) in enumerate(intervals):
        for cj, sj, ej in intervals[i + 1 :]:
            if sj >= ei:
                break  # sorted by start: no later commitment can overlap ci
            overlap = (min(ei, ej) - max(si, sj)).total_seconds() / 60.0
            if overlap > 0:
                conflicts.append(Conflict(a=ci, b=cj, overlap_minutes=round(overlap, 1)))
    return conflicts
