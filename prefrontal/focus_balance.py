"""Focus balance — a life-domain lens over your out-of-home time.

Closed-loop trip tracking (:mod:`prefrontal.trips`) answers *what* each undeclared
round trip was (its ``label``) and *what kind of activity* it was (its ``category``:
errand / social / health / …). This module adds the orthogonal axis the categories
don't capture: which **sphere of life** the time out served — ``shop`` / ``work`` /
``home`` / ``kids`` / ``personal`` — the same work/home *domain* todos and mail
already carry, so a work trip and a work todo roll up together.

It rolls up *both* halves of out-of-home time: passively-detected **trips** and
**declared outings** that returned (:mod:`prefrontal.modules.location_anchor`),
which never overlap in time — so together they cover every leave-home stretch.
Summed per domain over a week, that's a focus-balance read: am I spreading my
attention the way I mean to, or has one sphere quietly eaten the others? Optional
per-domain weekly **targets** (seeded by the Parent pack) turn that read into a
gentle "light on kids this week" nudge — well-being, not productivity: the point is
making sure home, kids, and personal get their share, not squeezing more out of the
day.

This file is the pure, testable core: the domain vocabulary and normalizer
(:func:`normalize_focus_domain`), the per-trip / per-outing domain resolution that
falls back to the activity category or the intention text so it works retroactively
(:func:`domain_of_trip` / :func:`domain_of_outing`), the rollup
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
    "domain_of_outing",
    "infer_domain_from_text",
    "read_targets",
    "target_state_key",
    "nudge_enabled",
    "DomainSpend",
    "FocusBalance",
    "build_focus_balance",
    "format_minutes",
    "balance_summary_line",
    "underserved_nudge_text",
    "STALE_LOCATION_HINT_DAYS",
    "UNASSIGNED_HINT_SHARE",
    "balance_hint",
]

#: The canonical life-spheres the rollup buckets time-out by. Free text is still
#: accepted on a trip (lives don't fit a fixed list), but these are what the label
#: form offers, what the Parent pack seeds vocabulary for, and what
#: :func:`normalize_focus_domain` snaps close spellings onto. ``home`` is household
#: (chores/errands for the house); ``kids`` is child-and-family time (school runs,
#: activities) — kept separate so a parent can see each on its own; ``personal``
#: folds in health, social, and leisure — everything that's time for *you*.
FOCUS_DOMAINS: tuple[str, ...] = ("shop", "work", "home", "kids", "personal")

#: Coaching-state key holding the ≤3 domains the trip-label ask's one-tap buttons
#: file into. ntfy caps action buttons at 3, so this lets a user surface the
#: spheres *they* most want to log (a shopkeeper wants ``shop``; a parent the
#: default trio) instead of a hard-coded set. Comma/space-separated; see
#: :func:`resolve_quick_domains`.
QUICK_DOMAINS_KEY = "trip_quick_domains"

#: The default quick-file trio when the key is unset/invalid — the three "protect"
#: spheres the balance feature exists to keep from being undercounted.
DEFAULT_QUICK_DOMAINS: tuple[str, ...] = ("home", "kids", "personal")


def resolve_quick_domains(store: Any) -> list[str]:
    """The ≤3 canonical domains the trip-label quick-file buttons should offer.

    Reads :data:`QUICK_DOMAINS_KEY` (comma/space-separated), snapping each token
    onto the canonical vocabulary (:func:`normalize_focus_domain`), keeping only
    real :data:`FOCUS_DOMAINS`, de-duplicating, and capping at 3 (ntfy's button
    limit). Falls back to :data:`DEFAULT_QUICK_DOMAINS` when the key is unset or
    yields nothing usable, so a bad value degrades to the old behavior rather than
    dropping the buttons.
    """
    raw = store.get_state(QUICK_DOMAINS_KEY, "") or ""
    picks: list[str] = []
    for token in raw.replace(",", " ").split():
        domain = normalize_focus_domain(token)
        if domain in FOCUS_DOMAINS and domain not in picks:
            picks.append(domain)
        if len(picks) == 3:
            break
    return picks or list(DEFAULT_QUICK_DOMAINS)


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


def infer_domain_from_text(text: str | None) -> str | None:
    """Best-effort life-domain from free text, or ``None`` when it's not clear-cut.

    Tokenizes the text and maps each word through the canonical vocab + synonyms;
    returns a domain only when **exactly one** distinct domain is implied — so
    "shop for parts" → shop and "swim with the kids" → kids, but an ambiguous or
    domain-less phrase ("grab a coffee", "meet the family lawyer downtown") stays
    ``None`` rather than being force-fit. Used to place a *declared outing* that
    carries no explicit ``domain`` (its only signal is the free-text intention),
    the looser counterpart to a trip's controlled-vocab category fallback.
    """
    if not text:
        return None
    found: set[str] = set()
    for raw in text.lower().replace("/", " ").split():
        word = "".join(ch for ch in raw if ch.isalpha())
        if not word:
            continue
        if word in FOCUS_DOMAINS:
            found.add(word)
        elif word in _DOMAIN_SYNONYMS:
            found.add(_DOMAIN_SYNONYMS[word])
    return next(iter(found)) if len(found) == 1 else None


def domain_of_outing(outing: dict[str, Any]) -> str | None:
    """The life-domain a declared outing counts toward: explicit → intention scan.

    Prefers the outing's explicitly-set ``domain`` (normalized); absent that, tries
    a conservative keyword scan of the free-text ``intention``
    (:func:`infer_domain_from_text`). ``None`` when neither is decisive — the rollup
    files that time as "unassigned" rather than guessing.
    """
    explicit = normalize_focus_domain(outing.get("domain"))
    if explicit:
        return explicit
    return infer_domain_from_text(outing.get("intention"))


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
    count: int  # number of trips + returned outings in this sphere
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
    """Roll out-of-home time over the last ``days`` up into per-domain totals.

    Both halves of "time out" count: passively-detected closed-loop **trips**
    (:mod:`prefrontal.trips`) and **declared outings** that returned
    (:mod:`prefrontal.modules.location_anchor`). The two never overlap in time — a
    passive trip only opens when no outing is active — so summing them is honest,
    and together they cover every leave-home → return-home stretch.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        now: Naive-UTC "now"; defaults to :func:`prefrontal.clock.utcnow`.
        days: Look-back window in days (default a week).
        targets: Per-domain weekly target minutes; defaults to
            :func:`read_targets`. A target is scaled to the window
            (``target × days / 7``) so a 3-day view compares against 3/7 of the aim.

    Returns:
        A :class:`FocusBalance`; domains sorted by minutes descending, then name.
        ``DomainSpend.count`` is the number of trips **and** returned outings in the
        sphere. A domain with a target but no time still appears (minutes 0), so a
        wholly neglected sphere shows up rather than silently vanishing.
    """
    now = now or utcnow()
    resolved_targets = read_targets(store) if targets is None else targets
    since = (now - timedelta(days=days)).strftime(TS_FMT)

    minutes: dict[str, float] = {}
    counts: dict[str, int] = {}

    def _add(domain: str | None, out: float | None) -> None:
        if out is None or out <= 0:
            return
        key = domain or "unassigned"
        minutes[key] = minutes.get(key, 0.0) + out
        counts[key] = counts.get(key, 0) + 1

    for trip in store.completed_trips_since(since):
        _add(
            domain_of_trip(trip),
            minutes_between(trip.get("departed_at"), trip.get("returned_at")),
        )
    for outing in store.completed_outings_since(since):
        _add(
            domain_of_outing(outing),
            minutes_between(outing.get("departure_at"), outing.get("returned_at")),
        )

    # Scale weekly targets to the actual window so a shorter view isn't unfairly
    # flagged as "behind" (and "unassigned" is never a target — it's the gap).
    window_scale = days / 7.0
    domains = set(minutes) | {d for d in resolved_targets if d != "unassigned"}
    spends = [
        DomainSpend(
            domain=d,
            minutes=round(minutes.get(d, 0.0), 1),
            count=counts.get(d, 0),
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


#: Days without a location ping before the balance flags "detection probably isn't
#: receiving pings". A quiet day or two is normal (you were home, or drove nowhere);
#: a stretch this long is a strong sign the phone's location automation stopped
#: rather than the user simply staying in.
STALE_LOCATION_HINT_DAYS = 3

#: When at least this share of in-window out-of-home time landed in ``unassigned``,
#: the balance is data-rich but sphere-poor — worth nudging the user to tag trips
#: rather than leaving the life-sphere view dominated by an untagged blob.
UNASSIGNED_HINT_SHARE = 0.5


def balance_hint(
    store: Any,
    balance: FocusBalance,
    *,
    trip_tracking_enabled: bool,
    now: datetime | None = None,
) -> str | None:
    """A one-line reason the focus balance looks empty or lopsided, or ``None``.

    Read-only diagnostics for ``GET /balance``: when the rollup is thin or skewed,
    name the most likely cause from the same state that gates trip detection, so an
    empty or single-sphere balance explains *itself* instead of just looking broken
    (the "why are no trips tracked / why only one sphere?" question this whole
    feature's plumbing can otherwise leave a mystery).

    Checks run most-fundamental first — each precondition gates the ones below it:

    1. **trip_tracking disabled** — nothing detects passive trips, so the balance
       reflects only declared outings. Surfaced only when the user actually expects
       the guardrail (a weekly target or the nudge flag is set), so a user who
       simply doesn't use trips isn't nagged.
    2. **no home set** — detection is dormant (no radius to cross).
    3. **no / stale location pings** — the phone isn't posting positions, so no loop
       can close (:data:`STALE_LOCATION_HINT_DAYS`).
    4. **a large ``unassigned`` slice** — trips *are* landing but untagged, so the
       life-sphere view is thin (:data:`UNASSIGNED_HINT_SHARE`).

    Returns ``None`` when the balance has healthy, mostly-assigned data — the common
    case, so a well-configured user sees no hint at all.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        balance: The rollup already computed for this window.
        trip_tracking_enabled: Whether the ``trip_tracking`` module is enabled for
            this deployment (passed in so this pure core needn't import the module
            registry).
        now: Naive-UTC "now" for the freshness check; defaults to
            :func:`prefrontal.clock.utcnow`.
    """
    if not trip_tracking_enabled:
        if nudge_enabled(store) or read_targets(store):
            return (
                "Trip tracking is off, so no round trips are being detected — this "
                "balance reflects only declared outings. Enable the trip_tracking "
                "module to populate it."
            )
        return None

    if store.get_home() is None:
        return (
            "No home location is set, so trip detection is dormant — set one "
            "(POST /webhooks/home) and leaving/returning starts logging automatically."
        )

    fresh = store.location_freshness(now=now)
    if fresh.get("location") is None:
        return (
            "No location pings received yet — trips are detected from your phone "
            "posting its position, so add the 'log location' automation to start."
        )
    age = fresh.get("age_seconds")
    if age is not None and age > STALE_LOCATION_HINT_DAYS * 86400:
        days = int(age // 86400)
        return (
            f"No location ping in {days} days — the balance can't see recent trips "
            "until your phone resumes posting its position."
        )

    if balance.has_data:
        unassigned = next(
            (d.minutes for d in balance.domains if d.domain == "unassigned"), 0.0
        )
        if unassigned > 0 and unassigned >= UNASSIGNED_HINT_SHARE * balance.total_minutes:
            return (
                "Much of your out-of-home time is unassigned — tag those trips with a "
                "life-sphere (shop/work/home/kids/personal) to sharpen the balance."
            )
    return None
