"""Focus balance — a life-domain lens over auto-detected trips.

Closed-loop trip tracking (:mod:`prefrontal.trips`) answers *what* each undeclared
round trip was (its ``label``) and *what kind of activity* it was (its ``category``:
errand / social / health / …). This module adds the orthogonal axis the categories
don't capture: which **sphere of life** the time out served — ``shop`` / ``work`` /
``home`` / ``personal`` — the same work/home *domain* todos and mail already carry,
so a work trip and a work todo roll up together.

Rolling the minutes you spent *out* per domain over a week is a focus-balance read:
am I spreading my attention the way I mean to, or has one sphere quietly eaten the
others? Optional per-domain weekly **targets** (seeded by the Parent pack) turn that
read into a gentle "light on home this week" nudge — this is well-being, not
productivity: the point is making sure home and personal get their share, not
squeezing more out of the day.

This file is the pure, testable core: the domain vocabulary and normalizer
(:func:`normalize_focus_domain`), the per-trip domain resolution that falls back to
the activity category so it works retroactively (:func:`domain_of_trip`), the rollup
(:func:`build_focus_balance`), and the phrasings the briefing / CLI / nudge share.
The HTTP surface (``/balance``) and the ambient nudge (the trip-tracking module)
call in here; nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.scheduling import minutes_between

__all__ = [
    "FOCUS_DOMAINS",
    "DEFAULT_BALANCE_DAYS",
    "normalize_focus_domain",
    "domain_of_trip",
    "read_targets",
    "target_state_key",
    "nudge_enabled",
    "DomainSpend",
    "FocusBalance",
    "build_focus_balance",
    "format_minutes",
    "balance_summary_line",
    "underserved_nudge_text",
]

#: The canonical life-spheres the rollup buckets time-out by. Free text is still
#: accepted on a trip (lives don't fit a fixed list), but these are what the label
#: form offers, what the Parent pack seeds vocabulary for, and what
#: :func:`normalize_focus_domain` snaps close spellings onto. ``home`` is household
#: (chores/errands for the house); ``kids`` is child-and-family time (school runs,
#: activities) — kept separate so a parent can see each on its own; ``personal``
#: folds in health, social, and leisure — everything that's time for *you*.
FOCUS_DOMAINS: tuple[str, ...] = ("shop", "work", "home", "kids", "personal")

#: Default look-back for the rollup — a week is the smallest window over which
#: "am I spreading my focus right?" is a fair question (a single day is too noisy).
DEFAULT_BALANCE_DAYS = 7

#: Free-text spellings that snap onto a canonical domain. Keeps "childcare"/"family"
#: from fragmenting the "kids" bucket, "house"/"chores" the "home" bucket, and
#: "business"/"store" from splitting "shop".
_DOMAIN_SYNONYMS: dict[str, str] = {
    "kid": "kids",
    "child": "kids",
    "children": "kids",
    "childcare": "kids",
    "family": "kids",
    "house": "home",
    "household": "home",
    "chores": "home",
    "errands": "home",
    "business": "shop",
    "store": "shop",
    "retail": "shop",
    "job": "work",
    "office": "work",
    "self": "personal",
    "me": "personal",
    "health": "personal",
    "wellbeing": "personal",
    "social": "personal",
    "leisure": "personal",
    "fun": "personal",
}

#: Trip *category* → life *domain* inference, for the unambiguous ones. Lets the
#: rollup place already-categorized trips before the user has tagged any domains,
#: so the feature has signal on day one. Deliberately partial: ``errand`` and
#: ``other`` are genuinely ambiguous (a shop supply run vs a home errand), so they
#: fall through to their own slice rather than being guessed into a sphere.
_CATEGORY_DOMAIN: dict[str, str] = {
    "work": "work",
    "family": "kids",
    "health": "personal",
    "social": "personal",
    "leisure": "personal",
}

#: State keys that turn the ambient weekly nudge on (:func:`nudge_enabled`). Seeded
#: "1" by the Parent pack; anything truthy here opts in.
_TRUTHY = {"1", "true", "on", "yes", "y"}


def normalize_focus_domain(value: str | None) -> str | None:
    """Snap a free-text domain onto the canonical vocabulary where it's obvious.

    A canonical domain returns as-is; a known synonym maps to its canonical form
    ("kids" → "home"); anything else is returned trimmed and lower-cased (free text
    is allowed); blank input returns ``None``. Mirrors
    :func:`prefrontal.trips.normalize_trip_category` so the two axes behave alike.
    """
    if not value or not value.strip():
        return None
    cleaned = value.strip().lower()
    if cleaned in FOCUS_DOMAINS:
        return cleaned
    if cleaned in _DOMAIN_SYNONYMS:
        return _DOMAIN_SYNONYMS[cleaned]
    if cleaned.endswith("s") and cleaned[:-1] in _DOMAIN_SYNONYMS:
        return _DOMAIN_SYNONYMS[cleaned[:-1]]
    return cleaned


def domain_of_trip(trip: dict[str, Any]) -> str | None:
    """The life-domain a trip counts toward: explicit ``domain`` → category fallback.

    Prefers the user's explicitly-tagged ``domain`` (normalized). Absent that, it
    infers from the activity ``category`` for the unambiguous ones (a ``work`` trip
    is work time; ``family`` is home; ``health``/``social``/``leisure`` are
    personal). A trip with neither returns ``None`` — the rollup shows that time as
    "unassigned" rather than guessing it into a sphere.
    """
    explicit = normalize_focus_domain(trip.get("domain"))
    if explicit:
        return explicit
    category = (trip.get("category") or "").strip().lower()
    if category in _CATEGORY_DOMAIN:
        return _CATEGORY_DOMAIN[category]
    return None


def target_state_key(domain: str) -> str:
    """Coaching-state key holding a domain's weekly target minutes."""
    return f"focus_target:{domain}"


def read_targets(store: Any) -> dict[str, float]:
    """Per-domain weekly target minutes from coaching state (``focus_target:<domain>``).

    Only positive, parseable values are returned; a blank/garbage/≤0 target is
    skipped (a target of zero is "don't track", not "aim for nothing"). The Parent
    pack seeds ``home``/``personal`` targets; the user can add/adjust any.
    """
    targets: dict[str, float] = {}
    for key, entry in store.all_state().items():
        if not key.startswith("focus_target:"):
            continue
        domain = key[len("focus_target:") :].strip().lower()
        if not domain:
            continue
        try:
            minutes = float(entry["value"])
        except (TypeError, ValueError, KeyError):
            continue
        if minutes > 0:
            targets[domain] = minutes
    return targets


def nudge_enabled(store: Any) -> bool:
    """Whether the ambient weekly focus-balance nudge is opted in (state flag)."""
    return (store.get_state("focus_balance_nudge") or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class DomainSpend:
    """Time out in one life-domain over the window, against its optional target."""

    domain: str
    minutes: float
    trips: int
    target_minutes: float | None = None

    @property
    def has_target(self) -> bool:
        return self.target_minutes is not None and self.target_minutes > 0

    @property
    def shortfall(self) -> float:
        """Minutes below target (0 when met or untargeted)."""
        if not self.has_target:
            return 0.0
        return max(0.0, self.target_minutes - self.minutes)  # type: ignore[operator]

    @property
    def underserved(self) -> bool:
        """A *targeted* domain that's under half its aim — the nudge-worthy gap."""
        return self.has_target and self.minutes < 0.5 * self.target_minutes  # type: ignore[operator]


@dataclass(frozen=True)
class FocusBalance:
    """The focus-balance rollup for a window: time out per life-domain."""

    days: int
    total_minutes: float
    domains: list[DomainSpend] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.domains) and self.total_minutes > 0

    def share(self, domain: str) -> float:
        """Fraction of total time-out in ``domain`` (0 when no data)."""
        if self.total_minutes <= 0:
            return 0.0
        for d in self.domains:
            if d.domain == domain:
                return d.minutes / self.total_minutes
        return 0.0

    def underserved(self) -> list[DomainSpend]:
        """Targeted domains sitting under half their weekly aim, biggest gap first."""
        gaps = [d for d in self.domains if d.underserved]
        return sorted(gaps, key=lambda d: -d.shortfall)


def build_focus_balance(
    store: Any,
    *,
    now: datetime | None = None,
    days: int = DEFAULT_BALANCE_DAYS,
    targets: dict[str, float] | None = None,
) -> FocusBalance:
    """Roll completed trips over the last ``days`` up into per-domain time-out.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        now: Naive-UTC "now"; defaults to :func:`prefrontal.clock.utcnow`.
        days: Look-back window in days (default a week).
        targets: Per-domain weekly target minutes; defaults to
            :func:`read_targets`. A target is scaled to the window
            (``target × days / 7``) so a 3-day view compares against 3/7 of the aim.

    Returns:
        A :class:`FocusBalance`; domains sorted by minutes descending, then name.
        A domain with a target but no trips still appears (minutes 0), so a wholly
        neglected sphere shows up rather than silently vanishing.
    """
    now = now or utcnow()
    resolved_targets = read_targets(store) if targets is None else targets
    since = (now - timedelta(days=days)).strftime(TS_FMT)

    minutes: dict[str, float] = {}
    counts: dict[str, int] = {}
    for trip in store.completed_trips_since(since):
        out = minutes_between(trip.get("departed_at"), trip.get("returned_at"))
        if out is None or out <= 0:
            continue
        domain = domain_of_trip(trip) or "unassigned"
        minutes[domain] = minutes.get(domain, 0.0) + out
        counts[domain] = counts.get(domain, 0) + 1

    # Scale weekly targets to the actual window so a shorter view isn't unfairly
    # flagged as "behind" (and "unassigned" is never a target — it's the gap).
    window_scale = days / 7.0
    domains = set(minutes) | {d for d in resolved_targets if d != "unassigned"}
    spends = [
        DomainSpend(
            domain=d,
            minutes=round(minutes.get(d, 0.0), 1),
            trips=counts.get(d, 0),
            target_minutes=(
                round(resolved_targets[d] * window_scale, 1)
                if d in resolved_targets
                else None
            ),
        )
        for d in domains
    ]
    spends.sort(key=lambda s: (-s.minutes, s.domain))
    return FocusBalance(
        days=days,
        total_minutes=round(sum(minutes.values()), 1),
        domains=spends,
    )


def format_minutes(minutes: float) -> str:
    """Human duration: ``40m`` under an hour, else ``6h`` / ``6h10`` (rounded)."""
    total = int(round(minutes))
    if total < 60:
        return f"{total}m"
    hours, mins = divmod(total, 60)
    return f"{hours}h" if mins == 0 else f"{hours}h{mins:02d}"


def balance_summary_line(balance: FocusBalance) -> str | None:
    """One-line "where your out-of-home time went" digest, or ``None`` with no data.

    E.g. ``Last 7d out (12h): shop 6h10, work 4h, home 1h30, personal 40m.``
    Sorted biggest-share first, so the sphere eating the week leads.
    """
    if not balance.has_data:
        return None
    parts = [
        f"{d.domain} {format_minutes(d.minutes)}"
        for d in balance.domains
        if d.minutes > 0
    ]
    if not parts:
        return None
    return (
        f"Last {balance.days}d out ({format_minutes(balance.total_minutes)}): "
        + ", ".join(parts)
        + "."
    )


def underserved_nudge_text(balance: FocusBalance) -> str | None:
    """Gentle "you're light on X" nudge for the most-neglected targeted domain(s).

    Returns ``None`` when nothing targeted is under half its aim — the common,
    quiet case. Names at most the top two gaps so it never reads as a scold.
    """
    gaps = balance.underserved()
    if not gaps:
        return None
    lead = gaps[0]
    got = format_minutes(lead.minutes)
    aim = format_minutes(lead.target_minutes or 0)
    if len(gaps) == 1:
        return (
            f"Light on {lead.domain} this week — {got} out against your {aim} aim. "
            "A short outing there would rebalance the week."
        )
    second = gaps[1]
    return (
        f"Light on {lead.domain} ({got} vs {aim}) and {second.domain} "
        f"({format_minutes(second.minutes)} vs {format_minutes(second.target_minutes or 0)}) "
        "this week — worth carving out a little time for each."
    )
