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

from typing import Any

from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.modules.location_anchor import DEFAULT_HOME_RADIUS_M, haversine_m

__all__ = [
    "TRIP_CATEGORIES",
    "DEFAULT_HOME_RADIUS_M",
    "normalize_trip_category",
    "heuristic_reflection_outcome",
    "classify_reflection",
    "process_location",
    "record_trip_return",
    "apply_reflection",
    "trip_label_prompt",
]

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
    for keywords, outcome in _REFLECTION_CUES:
        if any(keyword in lowered for keyword in keywords):
            return outcome
    return None


def parse_outcome_reply(reply: str) -> str | None:
    """Extract ``success``/``partial``/``miss`` from a model reply, or ``None``."""
    lowered = (reply or "").strip().lower()
    if not lowered:
        return None
    for word, outcome in _OUTCOME_WORDS.items():
        if word in lowered:
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


def process_location(
    store: Any,
    lat: float,
    lon: float,
    *,
    radius_m: float | None = None,
    home: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Fold a location ping into the closed-loop trip state machine.

    The single open trip (:meth:`~prefrontal.memory.store.MemoryStore.active_trip`)
    *is* the state: no active trip means "home", an active trip means "out". So:

    - **Away with no open trip** → **open** one (a *depart* edge), unless a
      *declared* outing is already tracking this trip (the Location-Aware Task
      Anchor owns those; we don't double-count).
    - **Away with an open trip** → grow its ``max_distance_m`` (still out).
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
        ``{"event", "trip_id", "distance_m", "at_home", "episode_id", "reason"}``
        where ``event`` is ``"depart"``/``"return"``/``None``. ``reason`` explains
        a ``None`` event when it's notable (``"no_home"``/``"outing_active"``).
    """
    home = home if home is not None else store.get_home()
    if home is None:
        return {"event": None, "reason": "no_home", "trip_id": None,
                "distance_m": None, "at_home": None, "episode_id": None}
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
    episode_id = store.log_episode(
        "task",
        actual_value=round(actual, 1) if actual is not None else None,
        acknowledged=False,
        context="trip",
        outcome=None,
        notes="closed-loop trip (auto-detected); awaiting reflection",
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


def trip_label_prompt(trip: dict[str, Any]) -> str:
    """Build the one-line "what was this trip?" ask for a completed, unlabeled trip.

    Args:
        trip: A completed trip dict (uses ``actual_minutes`` / ``max_distance_m``
            when present for a little grounding).
    """
    minutes = trip.get("actual_minutes")
    dist = trip.get("max_distance_m")
    bits = []
    if minutes is not None:
        bits.append(f"{round(minutes)} min")
    if dist:
        km = dist / 1000.0
        bits.append(f"{km:.1f} km out" if km >= 1 else f"{round(dist)} m out")
    span = f" ({', '.join(bits)})" if bits else ""
    return (
        f"You got back from a trip{span} — what was it? "
        "Tap to label and categorize it (and say how it went)."
    )
