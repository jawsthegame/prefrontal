"""Blockers — the pure, model-free helpers over "who's waiting on you".

The storage side is :class:`prefrontal.memory.repos.blockers.BlockersRepo`; this
module is the deterministic logic that shapes a raw blocker row into something a
surface can show — how long someone's been waiting, a one-line description, and
the "N people are waiting on you" digest line the morning briefing uses. Kept
pure (given ``now``, no clock or store) so it's trivially testable, the same
split :mod:`prefrontal.focus_balance` and :mod:`prefrontal.projects` use.

A blocker records that someone *else* is blocked until you do a thing — the
counterweight to shiny-object syndrome. The framing throughout is second-person
and non-judgmental: naming the wait, not nagging about it.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from prefrontal.clock import parse_ts as _parse_ts


def normalize_person(name: str) -> str:
    """Trim and collapse internal whitespace in a person's name."""
    return re.sub(r"\s+", " ", name or "").strip()


def waiting_days(blocking_since: Any, now: datetime) -> int:
    """Whole days someone has been waiting (``blocking_since`` → ``now``), floored at 0.

    A blocker logged today reads as ``0`` days ("since today"); a future
    ``blocking_since`` (shouldn't happen) also floors to ``0`` rather than going
    negative. Returns ``0`` when the timestamp can't be parsed.
    """
    since = _parse_ts(blocking_since)
    if since is None:
        return 0
    return max(0, int((now - since).total_seconds() // 86400))


def waited_phrase(blocking_since: Any, now: datetime) -> str:
    """Compact "how long they've waited" phrasing (``"today"`` / ``"3d"``)."""
    days = waiting_days(blocking_since, now)
    return "today" if days == 0 else f"{days}d"


def describe_blocker(blocker: dict[str, Any], now: datetime) -> str:
    """One line for a blocker: ``"Sam — the budget numbers (waiting 3d)"``."""
    person = normalize_person(blocker.get("person") or "Someone")
    what = (blocker.get("what") or "something").strip()
    return f"{person} — {what} (waiting {waited_phrase(blocker.get('blocking_since'), now)})"


def blocker_summary_line(open_blockers: list[dict[str, Any]], now: datetime) -> str | None:
    """A single "who's waiting on you" digest line, or ``None`` when nobody is.

    Leads with the count, then names the person who's waited longest (the one an
    unblock most obviously belongs to): e.g. ``"3 people are waiting on you —
    longest: Sam (5d)."`` Deterministic given ``now``; used by the morning
    briefing and any glanceable surface.
    """
    if not open_blockers:
        return None
    n = len(open_blockers)
    longest = max(open_blockers, key=lambda b: waiting_days(b.get("blocking_since"), now))
    person = normalize_person(longest.get("person") or "someone")
    waited = waited_phrase(longest.get("blocking_since"), now)
    noun = "person is" if n == 1 else "people are"
    return f"{n} {noun} waiting on you — longest: {person} ({waited})."
