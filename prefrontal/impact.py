"""Impact analysis — what gets thrown off when you're running behind.

When you overrun the current thing, the cost isn't local: being 8 minutes late on
coffee can make you late to a 10:30, which compresses prep for a 12:00. For
time-blindness, *seeing the downstream dominoes* is the motivator a bare timer
lacks. This module turns the schedule (`commitments`) plus a projected
free-time into a list of at-risk commitments.

It's **predictive** rather than reactive: the projected free-time is derived from
the learned `time_estimation_bias` (you say "back in 15", the bias is 1.4, so the
realistic projection is ~21 minutes) — so a conflict is flagged when you walk out
the door, not after you've already blown it.

Pure functions only; the outing check endpoint wires them to live data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.clock import parse_ts_strict as _parse


@dataclass(frozen=True)
class Impact:
    """How a projected free-time affects one commitment.

    Attributes:
        commitment: The commitment dict.
        latest_departure: UTC text — the latest you can leave and still make it
            on time (``start_at − lead_minutes``).
        slack_minutes: Minutes of slack; negative means you'd be late.
        at_risk: ``True`` when ``slack_minutes < 0``.
    """

    commitment: dict[str, Any]
    latest_departure: str
    slack_minutes: float
    at_risk: bool


def project_free_time(
    departure_at: str,
    window_minutes: float,
    bias: float,
    now: datetime | None = None,
) -> datetime:
    """Project when an active outing will realistically free you up.

    Applies the learned time bias to the stated window
    (``departure + window × bias``). If that realistic finish is already in the
    past (you're well over), the projection is ``now`` — you could leave now.

    Args:
        departure_at: UTC departure timestamp of the outing.
        window_minutes: The stated time window.
        bias: The ``time_estimation_bias`` multiplier (1.0 = no bias).
        now: Current naive-UTC time (defaults to :func:`utcnow`).

    Returns:
        The projected free-time as naive UTC.
    """
    now = now or utcnow()
    realistic = _parse(departure_at) + timedelta(minutes=window_minutes * bias)
    return max(now, realistic)


def analyze_impact(
    projected_free_at: datetime, commitments: list[dict[str, Any]]
) -> list[Impact]:
    """Compute slack for each commitment given when you'll be free, worst first.

    Args:
        projected_free_at: When you'll realistically be available (naive UTC).
        commitments: Upcoming commitment dicts (each with ``start_at`` and
            ``lead_minutes``).

    Returns:
        A list of :class:`Impact`, sorted by ``slack_minutes`` ascending (most
        at-risk first).
    """
    impacts: list[Impact] = []
    for c in commitments:
        start = _parse(c["start_at"])
        lead = c.get("lead_minutes") or 0.0
        latest = start - timedelta(minutes=lead)
        slack = (latest - projected_free_at).total_seconds() / 60.0
        impacts.append(
            Impact(
                commitment=c,
                latest_departure=latest.strftime(TS_FMT),
                slack_minutes=round(slack, 1),
                at_risk=slack < 0,
            )
        )
    impacts.sort(key=lambda i: i.slack_minutes)
    return impacts


def at_risk(impacts: list[Impact]) -> list[Impact]:
    """Filter to only the at-risk impacts (already worst-first)."""
    return [i for i in impacts if i.at_risk]


def impact_phrase(impacts: list[Impact]) -> str:
    """A short message tail naming the most-threatened commitment, or ``""``.

    Args:
        impacts: Impacts from :func:`analyze_impact`.

    Returns:
        e.g. ``" Heads up: 'Team sync' is now at risk (~6 min late)."`` — or an
        empty string when nothing is at risk.
    """
    risky = at_risk(impacts)
    if not risky:
        return ""
    top = risky[0]
    title = top.commitment.get("title", "your next commitment")
    late = round(-top.slack_minutes)
    return f" Heads up: '{title}' is now at risk (~{late} min late)."
