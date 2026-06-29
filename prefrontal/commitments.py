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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from prefrontal.memory.store import MemoryStore

#: Assumed duration for a commitment with no ``end_at`` (overlap detection).
DEFAULT_EVENT_MINUTES = 30.0


@dataclass(frozen=True)
class SyncSummary:
    """Result of a :func:`sync_calendar` run."""

    added: int
    updated: int
    cancelled: int
    upcoming: int
    conflicts: int
    new_conflict: bool


@dataclass(frozen=True)
class Conflict:
    """Two overlapping commitments — a double-booking."""

    a: dict[str, Any]
    b: dict[str, Any]
    overlap_minutes: float


def to_utc(value: str) -> str:
    """Normalize an ISO-8601 timestamp to ``YYYY-MM-DD HH:MM:SS`` in UTC.

    Accepts offset-aware (``2026-06-28T10:30:00-07:00``), ``Z``-suffixed, naive
    (assumed UTC), and date-only (``2026-06-28``, treated as midnight UTC) forms
    — matching what Google Calendar / CalDAV emit. The output format matches
    SQLite's ``datetime('now')`` so string comparison Just Works.

    Args:
        value: An ISO-8601 date or datetime string.

    Returns:
        A normalized UTC timestamp string.

    Raises:
        ValueError: If ``value`` is not a parseable ISO-8601 timestamp.
    """
    text = value.strip()
    # datetime.fromisoformat handles 'Z' as of Python 3.11.
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one inbound calendar/commitment event.

    Args:
        event: A raw event dict. Requires ``title`` and ``start_at``; optional
            ``external_id``, ``end_at``, ``location``, ``lead_minutes``, ``hard``.

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
    return {
        "title": title,
        "start_at": to_utc(event["start_at"]),
        "external_id": event.get("external_id") or None,
        "end_at": to_utc(end_raw) if end_raw else None,
        "location": event.get("location") or None,
        "lead_minutes": float(lead) if lead is not None else 10.0,
        "hardness": "hard" if event.get("hard") else "soft",
        "source": event.get("source") or "calendar",
    }


def sync_calendar(store: MemoryStore, events: list[dict[str, Any]]) -> SyncSummary:
    """Idempotently sync a batch of calendar events into ``commitments``.

    Upserts every provided event (by ``external_id``) and cancels future
    calendar commitments that are no longer present — so the stored schedule
    mirrors the calendar window. Manual commitments are untouched.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        events: Raw event dicts (see :func:`normalize_event`).

    Returns:
        A :class:`SyncSummary`.

    Raises:
        ValueError: If any event fails validation (the whole batch is rejected
            before any write, so a bad payload never partially applies).
    """
    normalized = [normalize_event(e) for e in events]  # validate all up front
    added = updated = 0
    keep: set[str] = set()
    for fields in normalized:
        _, created = store.upsert_commitment(**fields)
        if fields["external_id"]:
            keep.add(fields["external_id"])
        if created:
            added += 1
        else:
            updated += 1
    cancelled = store.cancel_missing_calendar(keep)
    upcoming = store.upcoming_commitments()
    conflicts = find_conflicts(upcoming)
    # Only flag a *new* conflict situation, so a standing double-booking doesn't
    # re-alert on every poll. We remember the last conflict signature and report
    # new_conflict only when the set changes to a non-empty one.
    signature = _conflict_signature(conflicts)
    new_conflict = bool(conflicts) and signature != store.get_state(
        "last_conflict_signature", ""
    )
    store.set_state("last_conflict_signature", signature, source="inferred")
    return SyncSummary(
        added=added,
        updated=updated,
        cancelled=cancelled,
        upcoming=len(upcoming),
        conflicts=len(conflicts),
        new_conflict=new_conflict,
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


def _parse_utc(ts: str) -> datetime:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` UTC timestamp."""
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


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
    items = sorted(commitments, key=lambda c: c["start_at"])
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
