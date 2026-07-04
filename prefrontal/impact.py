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

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.clock import parse_ts_strict as _parse

#: Assumed length of a commitment with no ``end_at`` — used only to propagate the
#: cascade *through* it. A typical meeting; a lateness arriving into an
#: unknown-length event carries forward by this much.
DEFAULT_COMMITMENT_MINUTES = 30.0


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


# -- Cascade / domino propagation --------------------------------------------
#
# `analyze_impact` scores every commitment against a *single* free-time, so a
# late finish on the current thing can only ever flag the one meeting it directly
# collides with. Reality dominoes: you show up late to the 10:30, the 10:30 still
# runs its half hour, so you leave it late too, and *that* is what threatens the
# 11:00. `cascade_impact` walks the day in order, carrying the running-late
# forward through each commitment's own duration, so the whole chain of knock-on
# risk is visible — not just the first collision.


@dataclass(frozen=True)
class CascadeImpact:
    """One commitment's place in the domino chain when you're running behind.

    Attributes:
        commitment: The commitment dict.
        projected_start: UTC text — when you'll *realistically* start this one,
            given every upstream commitment ran its full length first. Equals the
            scheduled ``start_at`` when nothing pushed it.
        delay_minutes: How far ``projected_start`` is past the scheduled start
            (``>= 0``); ``0`` means the cascade hasn't reached this one.
        slack_minutes: Minutes of slack against the propagated free-time; negative
            means you can't make it on time.
        at_risk: ``True`` when ``slack_minutes < 0``.
        caused_by: Title of the immediately-upstream commitment whose overrun
            pushed this one, or ``None`` when the root cause is the current
            overrun itself (the first domino) or the schedule was already tight.
    """

    commitment: dict[str, Any]
    projected_start: str
    delay_minutes: float
    slack_minutes: float
    at_risk: bool
    caused_by: str | None


def _commitment_duration(commitment: dict[str, Any], default_minutes: float) -> float:
    """Scheduled length in minutes — from ``end_at`` if present, else the default."""
    end = commitment.get("end_at")
    if not end:
        return default_minutes
    minutes = (_parse(end) - _parse(commitment["start_at"])).total_seconds() / 60.0
    return max(0.0, minutes)


def cascade_impact(
    projected_free_at: datetime,
    commitments: list[dict[str, Any]],
    default_duration_minutes: float = DEFAULT_COMMITMENT_MINUTES,
) -> list[CascadeImpact]:
    """Propagate a projected free-time through the schedule, domino by domino.

    Walks ``commitments`` in start order. Each is assumed attended in sequence:
    you leave when you're free, spend its ``lead_minutes`` getting there, then it
    runs its full length — so arriving late pushes its finish late, which becomes
    the free-time for the next one. The cascade self-heals: a commitment with
    enough slack before it starts on time, ending the chain until the next crunch.

    Args:
        projected_free_at: When you'll realistically be free for the *first* one
            (naive UTC) — e.g. from :func:`project_free_time` for an active outing.
        commitments: Upcoming commitment dicts (``start_at``, ``lead_minutes``,
            optional ``end_at``).
        default_duration_minutes: Length assumed for a commitment lacking
            ``end_at``, used only to carry the delay forward through it.

    Returns:
        A list of :class:`CascadeImpact` in schedule order (the reading order of
        the domino chain).
    """
    ordered = sorted(commitments, key=lambda c: _parse(c["start_at"]))
    free = projected_free_at
    pusher: str | None = None  # title of the upstream commitment now running late
    out: list[CascadeImpact] = []
    for c in ordered:
        start = _parse(c["start_at"])
        lead = c.get("lead_minutes") or 0.0
        latest_departure = start - timedelta(minutes=lead)
        slack = (latest_departure - free).total_seconds() / 60.0
        # You leave at `free`, travel `lead`, and can't start before the scheduled
        # time even if you arrive early.
        actual_start = max(start, free + timedelta(minutes=lead))
        delay = (actual_start - start).total_seconds() / 60.0
        out.append(
            CascadeImpact(
                commitment=c,
                projected_start=actual_start.strftime(TS_FMT),
                delay_minutes=round(delay, 1),
                slack_minutes=round(slack, 1),
                at_risk=slack < 0,
                caused_by=pusher if delay > 0 else None,
            )
        )
        # Carry forward: this one runs its length from when it actually starts.
        free = actual_start + timedelta(
            minutes=_commitment_duration(c, default_duration_minutes)
        )
        # If it ended late it becomes the next domino's cause; otherwise the chain
        # resets — a later commitment slipping is no longer this one's fault.
        pusher = c.get("title", "an earlier commitment") if delay > 0 else None
    return out


def cascade_at_risk(impacts: list[CascadeImpact]) -> list[CascadeImpact]:
    """Filter a cascade to only its at-risk links (schedule order preserved)."""
    return [i for i in impacts if i.at_risk]


def cascade_phrase(impacts: list[CascadeImpact]) -> str:
    """A short message tail describing the domino chain, or ``""``.

    One at-risk commitment reads like :func:`impact_phrase`; several are shown as
    the chain in time order (``A → B → C``, capped at three) so the knock-on is
    legible in a nudge.
    """
    risky = cascade_at_risk(impacts)
    if not risky:
        return ""
    if len(risky) == 1:
        top = risky[0]
        title = top.commitment.get("title", "your next commitment")
        late = round(-top.slack_minutes)
        return f" Heads up: '{title}' is now at risk (~{late} min late)."
    shown = risky[:3]
    chain = " → ".join(
        f"'{i.commitment.get('title', '?')}' (~{round(-i.slack_minutes)}m late)"
        for i in shown
    )
    more = "" if len(risky) <= 3 else f" +{len(risky) - 3} more"
    return f" This cascades: {chain}{more}."


def fragile_stretch(
    commitments: list[dict[str, Any]],
    bias: float,
    default_duration_minutes: float = DEFAULT_COMMITMENT_MINUTES,
) -> list[CascadeImpact]:
    """Preview which back-to-backs would topple if the day runs habitually long.

    Where :func:`cascade_impact` answers "given I'm *already* behind, what
    slips?", this is the morning-briefing preview: assume you start the first
    thing on time but every commitment runs ``bias`` × its planned length (your
    learned :data:`time_estimation_bias`), and see whether the chain still holds.
    A stretch that fits on paper but collides under that realistic inflation is
    what a morning heads-up wants to name — before the day starts.

    Seeds the cascade at the first commitment's on-time departure
    (``start − lead``), so the first is never itself flagged; only genuine
    knock-on from an inflated predecessor surfaces.

    Args:
        commitments: The commitments to preview (e.g. today's remaining ones).
        bias: The learned ``time_estimation_bias`` multiplier.
        default_duration_minutes: Length assumed for a commitment lacking
            ``end_at`` before inflation.

    Returns:
        The at-risk :class:`CascadeImpact` links (schedule order). Empty when the
        day has slack, when ``bias <= 1.0`` (no habitual overrun to model), or
        when there are fewer than two commitments to collide.
    """
    if bias <= 1.0 or len(commitments) < 2:
        return []
    ordered = sorted(commitments, key=lambda c: _parse(c["start_at"]))
    inflated: list[dict[str, Any]] = []
    for c in ordered:
        start = _parse(c["start_at"])
        stretched = _commitment_duration(c, default_duration_minutes) * bias
        inflated.append(
            {**c, "end_at": (start + timedelta(minutes=stretched)).strftime(TS_FMT)}
        )
    lead0 = ordered[0].get("lead_minutes") or 0.0
    seed = _parse(ordered[0]["start_at"]) - timedelta(minutes=lead0)
    chain = cascade_impact(seed, inflated, default_duration_minutes)
    # The overrun here is baked into the inflated durations, not modelled as a
    # start-delay, so cascade_impact's delay-based `caused_by` reads None. In this
    # contiguous preview the culprit is simply the commitment right before — the
    # one whose stretched length ate the buffer — so name it directly.
    out: list[CascadeImpact] = []
    for idx, link in enumerate(chain):
        if not link.at_risk:
            continue
        culprit = ordered[idx - 1].get("title") if idx > 0 else None
        out.append(replace(link, caused_by=culprit))
    return out
