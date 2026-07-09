"""Dependency-free ICS calendar fetch + parse (the no-n8n calendar path).

Prefrontal reads each user's own calendars from **private ICS feed URLs** — the
"secret address in iCal format" Google/Outlook expose — rather than an OAuth
integration. This module pulls a feed over HTTP and parses its ``VEVENT``\\ s into
the event dicts :func:`prefrontal.commitments.sync_calendar` consumes, so the
whole pipeline (recurrence expansion, TZID→UTC, conflict detection) is reused
unchanged.

It is a faithful in-app port of the old n8n ``calendar-sync`` "Parse ICS" nodes:
line unfolding, ``DTSTART;TZID=…`` handling, ``EXDATE``/``RECURRENCE-ID``,
namespaced ``external_id``\\ s, and dropping events the user has **declined**
(matched against their own addresses). Times pass through with their ``tzid``;
Prefrontal resolves the zone to UTC server-side (see
:func:`prefrontal.commitments.to_utc`).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

#: Fields extracted from a VEVENT, mapped to the sync event-dict keys.
_ICS_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?")


def fetch_ics(url: str, *, timeout: float = 30.0) -> str:
    """Fetch an ICS feed's text over HTTPS. Raises on a network/HTTP error."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _ics_date(value: str) -> str | None:
    """Convert an ICS date/datetime to ISO-8601, or ``None`` if unparseable.

    ``20260706`` → ``2026-07-06`` (all-day), ``20260706T090000Z`` →
    ``2026-07-06T09:00:00Z`` (UTC), ``20260706T090000`` →
    ``2026-07-06T09:00:00`` (naive; its zone travels separately in ``tzid``).
    """
    m = _ICS_DATE_RE.search(value)
    if not m:
        return None
    y, mo, d, h, mi, s, z = m.groups()
    if not h:
        return f"{y}-{mo}-{d}"
    if z:
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _tzid(params: str) -> str | None:
    """Pull the ``TZID`` param off a property's parameter text, or ``None``."""
    m = re.search(r'TZID="?([^:;"]+)', params)
    return m.group(1) if m else None


def _unfold(text: str) -> list[str]:
    """Unfold RFC 5545 continuation lines (a leading space/tab continues the prior)."""
    return text.replace("\r\n\t", "").replace("\r\n ", "").replace(
        "\n\t", ""
    ).replace("\n ", "").split("\n")


def parse_ics(
    text: str, *, namespace: str, me_emails: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Parse ICS text into event dicts for :func:`sync_calendar`.

    Args:
        text: The raw ICS document.
        namespace: Feed slug used to namespace ``external_id`` (e.g. ``"work"``),
            so pruning one feed never cancels another's events. Stored as
            ``<namespace>:<UID>``.
        me_emails: The user's own addresses (any case); an event they've marked
            ``PARTSTAT=DECLINED`` is dropped. Empty disables declined-filtering.

    Returns:
        Event dicts (``title``/``start_at``/``end_at``/``tzid``/``external_id``/
        ``rrule``/``exdate``/``recurrence_id``/``location``/``url``), skipping
        events without a start, declined events, and cancelled events.
    """
    me = {e.strip().lower() for e in me_emails if e.strip()}
    ns = namespace.rstrip(":")
    lines = _unfold(text.replace("\r\n", "\n"))
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    declined = cancelled = False
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "BEGIN:VEVENT":
            cur = {}
            declined = cancelled = False
            continue
        if line == "END:VEVENT":
            if cur is not None and cur.get("start_at") and not declined and not cancelled:
                # Titleless VEVENTs (private/busy blocks) are valid ICS; treat them
                # as a "Busy" hold rather than rejecting the whole feed batch.
                if not (cur.get("title") or "").strip():
                    cur["title"] = "Busy"
                out.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        idx = line.find(":")
        if idx < 0:
            continue
        params = line[:idx]
        name = params.split(";")[0]
        val = line[idx + 1 :]
        if name == "UID":
            cur["external_id"] = f"{ns}:{val}"
        elif name == "SUMMARY":
            cur["title"] = val
        elif name == "LOCATION":
            cur["location"] = val
        elif name == "URL":
            cur["url"] = val
        elif name == "RRULE":
            cur["rrule"] = val
        elif name == "STATUS":
            cancelled = val.strip().upper() == "CANCELLED"
        elif name == "EXDATE":
            for part in val.split(","):
                d = _ics_date(part)
                if d is not None:
                    cur.setdefault("exdate", []).append(d)
        elif name == "RECURRENCE-ID":
            cur["recurrence_id"] = _ics_date(val)
        elif name == "DTSTART":
            cur["start_at"] = _ics_date(val)
            cur["tzid"] = _tzid(params)
        elif name == "DTEND":
            cur["end_at"] = _ics_date(val)
            cur["end_tzid"] = _tzid(params)
        elif name == "ATTENDEE" and me:
            email = re.sub(r"^mailto:", "", val, flags=re.I).strip().lower()
            if email in me and re.search(r"PARTSTAT=DECLINED", params, re.I):
                declined = True
    return out
