"""Closed-loop trip tracking — the passive, retrospective counterpart to the
Location-Aware Task Anchor (:mod:`prefrontal.modules.location_anchor`).

An *outing* is something you **declare** up front ("coffee, back in 15") and get
nudged about in real time. A *trip* is the opposite: nobody declared anything.
The system watches location pings (`POST /webhooks/location`) and, when the phone
crosses out of the home radius and later crosses back in, records that round trip
as a **closed loop** — a trip you took without telling it. Because it was never
declared, the useful moment is *afterwards*: the system asks you to **label** and
**categorize** the trip, and lets you say, in plain English, **how it went**. That
honest reflection is classified into an outcome that resolves the trip's episode,
so it feeds the same learning loop everything else does — and the raw note is also
handed to the LLM-as-sensor (:mod:`prefrontal.sensor`) to *propose* deeper
structured updates a human still confirms.

This file holds the pure, testable core: the home-radius state machine
(:func:`process_location`), the reflection classifier (LLM-first with a keyword
heuristic fallback, mirroring :func:`prefrontal.classify.classify_kind`), the
category vocabulary, and the episode-recording helpers. The HTTP surface
(``/webhooks/trip/*``, ``/trips``) lives in :mod:`prefrontal.webhooks.routers`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.geo import (
    DEFAULT_HOME_RADIUS_M,
    DEFAULT_PLACE_MATCH_RADIUS_M,
    haversine_m,
    nearest_place,
)
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.scheduling import minutes_between

__all__ = [
    "TRIP_CATEGORIES",
    "DEFAULT_HOME_RADIUS_M",
    "DEFAULT_STOP_RADIUS_M",
    "DEFAULT_DWELL_MINUTES",
    "normalize_trip_category",
    "heuristic_reflection_outcome",
    "classify_reflection",
    "process_location",
    "record_trip_return",
    "apply_reflection",
    "suggest_trip_labeling",
    "trip_label_prompt",
]

#: How close two fixes must be (metres) to count as "the same stop" — inside this
#: the phone is dwelling at one place, beyond it it has moved on to a new spot.
#: Tunable via the ``trip_stop_radius_m`` coaching key.
DEFAULT_STOP_RADIUS_M = 150.0
#: How long (minutes) the phone must linger at a spot before it counts as a real
#: *stop* (a leg boundary) rather than passing through — a red light or slow
#: traffic shouldn't split a trip. Tunable via ``trip_dwell_minutes``.
DEFAULT_DWELL_MINUTES = 8.0

#: Suggested categories a trip can be filed under. Free text is still accepted
#: (people's lives don't fit a fixed list), but these are what the label prompt
#: offers and what :func:`normalize_trip_category` snaps close spellings onto.
TRIP_CATEGORIES: tuple[str, ...] = (
    "errand",
    "social",
    "work",
    "health",
    "family",
    "leisure",
    "other",
)




def normalize_trip_category(value: str | None) -> str | None:
    """Snap a free-text category onto the canonical vocabulary where it's obvious.

    A case-insensitive exact match returns the canonical spelling; anything else
    is returned trimmed and lower-cased (free text is allowed), and blank input
    returns ``None``. Keeps ``ERRAND`` / ``Errands`` from fragmenting the stats
    while never rejecting a category the user genuinely wants.
    """
    if not value or not value.strip():
        return None
    cleaned = value.strip().lower()
    if cleaned in TRIP_CATEGORIES:
        return cleaned
    # Tolerate a trailing plural ("errands" -> "errand").
    if cleaned.endswith("s") and cleaned[:-1] in TRIP_CATEGORIES:
        return cleaned[:-1]
    return cleaned


# --- Reflection → outcome ----------------------------------------------------
#
# "How did it go?" in plain English, mapped to the drift vocabulary
# (success/partial/miss) so an honest self-report feeds the learning loop. Like
# the rest of the LLM surface here, the model leads and a keyword heuristic
# backs it up — so a down model never means no signal.

#: Keyword groups → outcome for the heuristic. Ordered most-negative first so a
#: "went fine but ran way over" reads as the honest "partial", not a false
#: "success" — the miss/partial cues win ties over the upbeat ones.
_REFLECTION_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "way longer", "way too long", "took forever", "ran over", "ran late",
            "late", "distracted", "sidetracked", "forgot", "disaster", "stressful",
            "stressed", "waste", "wasted", "annoying", "frustrating", "rough",
            "terrible", "awful", "failed", "fell apart", "spiralled", "spiraled",
            "lost track", "blew ", "overdid",
        ),
        "miss",
    ),
    (
        (
            "longer than", "bit long", "a bit", "kind of", "sort of", "mostly",
            "so-so", "meh", "mixed", "okay", "ok ", "not bad", "could have been",
            "could've been", "slower than", "more than i", "over the",
        ),
        "partial",
    ),
    (
        (
            "went well", "great", "smooth", "smoothly", "quick", "quickly",
            "on time", "productive", "nailed", "easy", "perfect", "fast",
            "good", "nice", "efficient", "in and out", "no problem", "fine",
        ),
        "success",
    ),
)

#: The cue groups above, precompiled to word-boundary regexes so a cue matches a
#: whole word/phrase — not a fragment inside an unrelated word ("late" in
#: "related", "fast" in "breakfast", "good" in "goodbye", "a bit" in "a bitter").
#: Trailing-space cues ("blew ", "ok ") strip cleanly since ``\b`` supplies the
#: boundary. Compiled once at import; matching is then a single regex per group.
_REFLECTION_CUE_RES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(r"\b(?:" + "|".join(re.escape(k.strip()) for k in keywords) + r")\b"),
        outcome,
    )
    for keywords, outcome in _REFLECTION_CUES
)

REFLECTION_SYSTEM_PROMPT = (
    "You read a short, honest note about how a trip/errand went and classify it "
    "as exactly one word: SUCCESS (it went to plan — on time, no drift), PARTIAL "
    "(it got done but ran long or was a bit off), or MISS (it went badly — much "
    "longer than meant, derailed, or a bust). Answer with only that one word."
)

_OUTCOME_WORDS = {"success": "success", "partial": "partial", "miss": "miss"}


def heuristic_reflection_outcome(text: str) -> str | None:
    """Classify a plain-English reflection into an outcome by keyword, or ``None``.

    Args:
        text: The free-text "how it went" note.

    Returns:
        ``"success"``/``"partial"``/``"miss"`` for the first matching cue group
        (most-negative first), or ``None`` when nothing matches.
    """
    lowered = (text or "").lower()
    for pattern, outcome in _REFLECTION_CUE_RES:
        if pattern.search(lowered):
            return outcome
    return None


def parse_outcome_reply(reply: str) -> str | None:
    """Extract ``success``/``partial``/``miss`` from a model reply, or ``None``."""
    lowered = (reply or "").strip().lower()
    if not lowered:
        return None
    for word, outcome in _OUTCOME_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):  # "miss" not inside "dismiss"
            return outcome
    return None


def classify_reflection(
    text: str, *, client: Generator | None = None
) -> tuple[str | None, str]:
    """Classify a reflection into an outcome; ``(outcome, source)``.

    Tries the local model first (``source="llm"``), then the keyword heuristic
    (``"heuristic"``); if neither is decisive returns ``(None, "none")`` — an
    honest no-guess, so a vague note ("was out for a while") is stored without a
    fabricated verdict.

    Args:
        text: The free-text reflection.
        client: An Ollama-like client; ``None`` skips the model.
    """
    if not text or not text.strip():
        return (None, "none")
    if client is not None:
        try:
            reply = client.generate(text.strip(), system=REFLECTION_SYSTEM_PROMPT)
            outcome = parse_outcome_reply(reply)
        except OllamaError:
            outcome = None
        if outcome is not None:
            return (outcome, "llm")
    heuristic = heuristic_reflection_outcome(text)
    if heuristic is not None:
        return (heuristic, "heuristic")
    return (None, "none")


# --- Location state machine --------------------------------------------------


def _update_dwell(
    store: Any,
    trip: dict[str, Any],
    lat: float,
    lon: float,
    home: dict[str, float],
    now: datetime,
    *,
    stop_radius_m: float,
    dwell_minutes: float,
) -> int | None:
    """Advance a trip's dwell state for one away ping; return a new waypoint id, if any.

    Tracks a single "stop candidate" on the trip (where the phone is currently
    lingering). Each away ping either **holds** the candidate (still within
    ``stop_radius_m``) — and, once the dwell there passes ``dwell_minutes``,
    promotes it to a :meth:`add_trip_waypoint` stop exactly once — or **resets** it
    to the new position (the phone moved on). So a chained run (home → store →
    school → home) lays down a waypoint per real stop, while passing through
    (traffic, a red light) never lingers long enough to count.
    """
    now_str = now.strftime(TS_FMT)
    cand_lat = trip.get("cand_lat")
    cand_lon = trip.get("cand_lon")
    cand_since = trip.get("cand_since")
    if cand_lat is None or cand_lon is None or cand_since is None:
        store.set_trip_stop_candidate(trip["id"], lat, lon, now_str)
        return None
    if haversine_m(cand_lat, cand_lon, lat, lon) > stop_radius_m:
        # Moved on from the candidate spot — start a fresh candidate here.
        store.set_trip_stop_candidate(trip["id"], lat, lon, now_str)
        return None
    # Still around the candidate: accumulate dwell and promote once it's a real stop.
    if trip.get("cand_logged"):
        return None
    dwelled = minutes_between(cand_since, now_str)
    if dwelled is None or dwelled < dwell_minutes:
        return None
    dist_from_home = round(haversine_m(home["lat"], home["lon"], cand_lat, cand_lon))
    waypoint_id = store.add_trip_waypoint(
        trip["id"], cand_lat, cand_lon, dist_from_home, cand_since
    )
    store.mark_trip_candidate_logged(trip["id"])
    return waypoint_id


def process_location(
    store: Any,
    lat: float,
    lon: float,
    *,
    radius_m: float | None = None,
    home: dict[str, float] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold a location ping into the closed-loop trip state machine.

    The single open trip (:meth:`~prefrontal.memory.store.MemoryStore.active_trip`)
    *is* the state: no active trip means "home", an active trip means "out". So:

    - **Away with no open trip** → **open** one (a *depart* edge), unless a
      *declared* outing is already tracking this trip (the Location-Aware Task
      Anchor owns those; we don't double-count).
    - **Away with an open trip** → grow its ``max_distance_m`` (still out) and
      advance dwell detection (:func:`_update_dwell`), which lays down an
      intermediate **waypoint** each time the phone lingers somewhere away from
      home long enough to count as a stop — so a chained errand run is split into
      its legs rather than collapsing into one blob.
    - **Home with an open trip** → **close** it (a *return* edge — the loop is now
      closed) and log a ``task`` episode for the learning loop.
    - **Home with no open trip** → nothing (still home).

    Pure state machine over the store; performs the writes but decides nothing the
    caller can't see in the returned dict.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        lat: The ping's latitude.
        lon: The ping's longitude.
        radius_m: Home radius; defaults to the ``home_radius_m`` state (then
            :data:`DEFAULT_HOME_RADIUS_M`).
        home: The home coordinate; defaults to :meth:`MemoryStore.get_home`.

    Returns:
        ``{"event", "trip_id", "distance_m", "at_home", "episode_id", "reason",
        "waypoint_id"}`` where ``event`` is ``"depart"``/``"return"``/``None`` and
        ``waypoint_id`` is the id of an intermediate stop recorded on this ping (or
        ``None``). ``reason`` explains a ``None`` event when it's notable
        (``"no_home"``/``"outing_active"``).
    """
    now = now or utcnow()
    home = home if home is not None else store.get_home()
    if home is None:
        return {"event": None, "reason": "no_home", "trip_id": None,
                "distance_m": None, "at_home": None, "episode_id": None,
                "waypoint_id": None}
    if radius_m is None:
        radius_m = store.get_float("home_radius_m", DEFAULT_HOME_RADIUS_M)

    distance_m = round(haversine_m(home["lat"], home["lon"], lat, lon))
    at_home = distance_m <= radius_m
    active = store.active_trip()

    result: dict[str, Any] = {
        "event": None,
        "reason": None,
        "trip_id": active["id"] if active else None,
        "distance_m": distance_m,
        "at_home": at_home,
        "episode_id": None,
        "waypoint_id": None,
    }

    if at_home:
        if active is not None:
            closed = store.close_trip(active["id"])
            result["event"] = "return"
            result["trip_id"] = active["id"]
            if closed is not None:
                result["episode_id"] = record_trip_return(store, closed)
        return result

    # Away from home.
    if active is not None:
        store.bump_trip_distance(active["id"], distance_m)
        stop_radius = store.get_float("trip_stop_radius_m", DEFAULT_STOP_RADIUS_M)
        dwell = store.get_float("trip_dwell_minutes", DEFAULT_DWELL_MINUTES)
        result["waypoint_id"] = _update_dwell(
            store, active, lat, lon, home, now,
            stop_radius_m=stop_radius, dwell_minutes=dwell,
        )
        return result
    # A declared outing already tracks this trip — don't open a passive duplicate.
    if store.active_outings():
        result["reason"] = "outing_active"
        return result
    trip_id = store.open_trip(depart_lat=home["lat"], depart_lon=home["lon"])
    store.bump_trip_distance(trip_id, distance_m)
    result["event"] = "depart"
    result["trip_id"] = trip_id
    return result


def record_trip_return(store: Any, closed: dict[str, Any]) -> int | None:
    """Log a completed trip as a *pending* ``task`` episode; return its id.

    ``actual_value`` is the minutes the loop took; ``predicted_value`` is ``None``
    (nothing was estimated — a trip is undeclared), so it never pollutes the
    shared ``time_estimation_bias`` (which needs both). ``outcome`` is left
    ``None`` — the honest verdict comes from the user's reflection later
    (:func:`apply_reflection`), which resolves this episode into the drift signal.

    Returns the new episode id, and links it onto the trip so the reflection can
    find it. ``None`` (no episode) only when the trip has no measured duration.
    """
    actual = closed.get("actual_minutes")
    stops = len(store.trip_waypoints(closed["id"]))
    stop_note = f"; {stops} stop{'s' if stops != 1 else ''}" if stops else ""
    episode_id = store.log_episode(
        "task",
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=False,
        context="trip",
        outcome=None,
        notes=f"closed-loop trip (auto-detected){stop_note}; awaiting reflection",
    )
    store.set_trip_episode(closed["id"], episode_id)
    return episode_id


def apply_reflection(
    store: Any,
    trip: dict[str, Any],
    reflection: str,
    *,
    outcome: str | None = None,
    client: Generator | None = None,
    sensor_client: Any = None,
) -> dict[str, Any]:
    """Record a plain-English reflection on a trip and feed it back into learning.

    Three things happen, in order:

    1. The reflection is classified into an outcome (``outcome`` overrides the
       classifier when the user states it explicitly) and stored on the trip.
    2. If an outcome was determined and the trip's return logged an episode, that
       episode is **resolved** to the outcome (:meth:`MemoryStore.set_episode_outcome`)
       — so honest self-report becomes drift signal the learn pass reads.
    3. The raw note is handed to the LLM-as-sensor to *propose* deeper structured
       updates (pending proposals a human still confirms) — best-effort; a down
       model simply proposes nothing.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        trip: The trip dict (must carry ``id``; ``episode_id`` when it has one).
        reflection: The free-text "how it went" note.
        outcome: Optional explicit outcome from the user (skips classification).
        client: Ollama-like client for the outcome classifier.
        sensor_client: Ollama client for the sensor extraction; ``None`` skips it.

    Returns:
        ``{"outcome", "outcome_source", "episode_resolved", "proposals"}``.
    """
    if outcome in ("success", "partial", "miss"):
        resolved_outcome, source = outcome, "explicit"
    else:
        resolved_outcome, source = classify_reflection(reflection, client=client)

    store.set_trip_reflection(trip["id"], reflection=reflection, outcome=resolved_outcome)

    episode_resolved = False
    episode_id = trip.get("episode_id")
    if resolved_outcome is not None and episode_id is not None:
        episode_resolved = store.set_episode_outcome(
            episode_id, outcome=resolved_outcome, acknowledged=True
        )

    # Deeper structured signal: let the sensor *propose* (never write) from the
    # raw note. Import locally so the core has no hard dependency on the sensor.
    proposals: list[int] = []
    if sensor_client is not None:
        from prefrontal.sensor import extract_candidates, record_candidates

        candidates = extract_candidates(reflection, client=sensor_client)
        proposals = record_candidates(store, candidates)

    return {
        "outcome": resolved_outcome,
        "outcome_source": source,
        "episode_resolved": episode_resolved,
        "proposals": proposals,
    }


def suggest_trip_labeling(
    store: Any,
    trip: dict[str, Any],
    *,
    radius_m: float | None = None,
    places: list[dict[str, Any]] | None = None,
    waypoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Reverse-match a completed trip's stops to a curated place — a label/domain guess.

    The passive counterpart to naming a trip by hand: a trip records a **waypoint**
    for each place the phone dwelt at long enough to count as a stop
    (:func:`process_location`), so a stop that lands at one of the user's curated
    places (:meth:`~prefrontal.memory.repos.schedule.ScheduleRepo.places`) *is* a
    destination they've already named. This snaps that coordinate back onto the
    place (:func:`prefrontal.geo.nearest_place`) and returns a labeling suggestion
    the ask can pre-fill — so a recurring destination is confirmed with a tap rather
    than typed, and a place tagged with a life-sphere pre-files the focus-balance
    domain too.

    The **farthest-from-home** matched stop wins — that's the trip's real
    destination (a home → shop → home run should read as "shop", not the corner the
    phone paused at). Best-effort and offline (curated places only, no network
    reverse-geocode): ``None`` when the trip has no stops, no places are curated, or
    no stop lands within the match radius.

    Reads are ordered cheapest-signal-first and every input is prefetchable, so a
    caller annotating many trips per tick (the ``/trips`` list, the label-ask cue)
    can avoid an N+1: waypoints are checked *before* places/radius, so a
    no-stop trip costs nothing further, and ``places``/``radius_m``/``waypoints`` can
    each be passed in to reuse a single read across the batch.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        trip: The completed trip dict (needs ``id``).
        radius_m: Match radius in metres; defaults to the ``place_match_radius_m``
            coaching key, then :data:`~prefrontal.geo.DEFAULT_PLACE_MATCH_RADIUS_M`.
        places: Preloaded curated places to match against; ``None`` reads them from
            the store (pass the shared list when annotating several trips at once).
        waypoints: Preloaded stops for this trip; ``None`` reads them from the store
            (pass them to reuse a read the caller already did, e.g. for a stop count).

    Returns:
        ``{"place", "label", "domain", "distance_m"}`` for the best match (``domain``
        may be ``None`` if the place carries no sphere), or ``None``.
    """
    # Cheapest signal first: no stops → nothing to match, and we skip the places/
    # radius reads entirely (the common short trip that never dwelt anywhere).
    raw_waypoints = waypoints if waypoints is not None else store.trip_waypoints(trip["id"])
    if not raw_waypoints:
        return None
    resolved_places = places if places is not None else store.places()
    if not resolved_places:
        return None
    if radius_m is None:
        radius_m = store.get_float("place_match_radius_m", DEFAULT_PLACE_MATCH_RADIUS_M)
    # Destination-first: the farthest stop from home is the most informative label.
    stops = sorted(raw_waypoints, key=lambda w: -(w.get("distance_m") or 0))
    for stop in stops:
        lat, lon = stop.get("lat"), stop.get("lon")
        if lat is None or lon is None:
            continue
        match = nearest_place(resolved_places, lat, lon, radius_m=radius_m)
        if match is not None:
            place, dist = match
            return {
                "place": place.get("name"),
                "label": place.get("label") or place.get("name"),
                "domain": place.get("domain"),
                "distance_m": round(dist),
            }
    return None


def trip_label_prompt(
    trip: dict[str, Any], *, suggestion: dict[str, Any] | None = None
) -> str:
    """Build the one-line "what was this trip?" ask for a completed, unlabeled trip.

    Args:
        trip: A completed trip dict (uses ``actual_minutes`` / ``max_distance_m``
            when present for a little grounding).
        suggestion: An optional reverse-match guess from
            :func:`suggest_trip_labeling`; when present the ask leads with it ("Looks
            like <place> — tap to confirm") so a recognized destination is a
            one-tap confirm rather than a cold prompt.
    """
    minutes = trip.get("actual_minutes")
    dist = trip.get("max_distance_m")
    stops = trip.get("stop_count") or 0
    bits = []
    if minutes is not None:
        bits.append(f"{round(minutes)} min")
    if dist:
        km = dist / 1000.0
        bits.append(f"{km:.1f} km out" if km >= 1 else f"{round(dist)} m out")
    if stops:
        bits.append(f"{stops} stop{'s' if stops != 1 else ''}")
    span = f" ({', '.join(bits)})" if bits else ""
    if suggestion and suggestion.get("label"):
        domain = suggestion.get("domain")
        sphere = f" ({domain})" if domain else ""
        lead = (
            f"You got back from a trip{span} — looks like {suggestion['label']}{sphere}? "
            "Tap to confirm, relabel, or say how it went."
        )
    else:
        lead = (
            f"You got back from a trip{span} — what was it? "
            "Tap to label it, file it (shop/work/home/kids/personal), and say how it went."
        )
    multi = (
        " Looks like a few stops — tell me about each if they were different errands."
        if stops >= 2
        else ""
    )
    return f"{lead}{multi}"
