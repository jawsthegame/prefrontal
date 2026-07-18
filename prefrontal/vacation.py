"""Vacation mode — ease off the nudges while the user is away.

See the design (:doc:`docs/design/vacation-mode.md`) for the full rationale. In
short: vacation mode is a **receptivity profile, not a kill switch**. While it's
on, the coaching engine holds every *discretionary* cue — the same non-critical,
non-user-initiated set quiet hours holds — but for a multi-day, location-cued
stretch instead of a nightly clock window. Genuinely time-critical real-world
obligations (a flight on the calendar) are ``critical`` and still get through, as
they do through quiet hours; on-demand surfaces (panic, emotion support) are
never gated at all. Vacation ≠ off.

The design resolves "automatic-by-location vs. manual toggle" toward **cue-based
detect-and-confirm with a manual override**, and this module implements the two
halves that split cleanly by confidence:

- **Entry is manual / conservative.** The user flips it on
  (``prefrontal vacation on`` / ``POST /vacation``). Auto-*switching* on entry is
  deliberately not done — a long day-trip or a work offsite shouldn't silently
  mute the assistant; the location-cued *suggestion* (a one-tap confirm) is the
  tracked follow-up, not a silent state change.
- **Exit leans automatic.** Returning inside the home radius is a
  high-confidence "it's over" cue, so the trip state machine calls
  :func:`resume_on_return` to clear an active vacation. This is the
  safety-critical half — a *forgotten* manual off is what turns the tool into a
  permanent mute (commandment 9's own corollary: "a muted tool is a dead tool"),
  and location closes that gap for free. A staycation never departs, so its loop
  never closes and this never fires — the return edge only exists after a real
  trip.

Pure and deterministic: every time-derived value is threaded in by the caller
(no ``utcnow`` in the core), so a test that sets the same inputs always gets the
same state. State lives in the per-user ``coaching_state`` key-value store, so
there is no new schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from prefrontal.clock import TS_FMT

#: Coaching-state key: whether vacation mode is on for this user ("1"/"0").
VACATION_ACTIVE_KEY = "vacation_active"
#: Coaching-state key: when the current vacation started (``TS_FMT``, UTC).
#: Display-only ("eased since Tue"); preserved across a re-activate so the "since"
#: clock reflects the *start* of the stretch, not the last toggle.
VACATION_SINCE_KEY = "vacation_since"
#: Coaching-state key: what turned it on — ``"manual"`` or ``"auto"`` (a confirmed
#: location suggestion). Recorded for explainability, not gating.
VACATION_SOURCE_KEY = "vacation_source"

#: The ``source`` values :func:`activate` stamps on :data:`VACATION_SOURCE_KEY`.
#: ``manual`` is a user toggle (CLI / ``POST /vacation``); ``auto`` is a confirmed
#: location *entry* suggestion (:func:`should_suggest_vacation` → a one-tap
#: ``vacation_confirm``). Note the *exit* is not sourced — :func:`resume_on_return`
#: calls :func:`deactivate`, which clears the state to a clean slate rather than
#: recording who lifted it.
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"

#: Default away-from-home dwell before the *entry* suggestion fires, in nights. Two
#: nights is deliberately conservative: an errand or a single overnight never trips
#: it, only a genuine multi-day trip does — the design's "suggest, don't switch, and
#: don't false-positive a work day-trip into silence" stance. Tunable per user via
#: :data:`VACATION_SUGGEST_AFTER_KEY`.
DEFAULT_SUGGEST_AFTER_NIGHTS = 2
#: Coaching-state key overriding :data:`DEFAULT_SUGGEST_AFTER_NIGHTS`.
VACATION_SUGGEST_AFTER_KEY = "vacation_suggest_after_nights"

#: Dedup-key prefix for the one-per-absence entry suggestion (keyed on trip id), so
#: the engine's fire-once guard suggests at most once per trip — a single ask, never
#: a recurring nag (commandments 9 & 10).
SUGGEST_DEDUP_PREFIX = "vacation_suggest"


def is_on_vacation(store: Any) -> bool:
    """Whether vacation mode is currently on for the store's user.

    The one read the coaching engine needs (threaded onto the per-tick context in
    :func:`prefrontal.coaching.build_context`). Best-effort like the rest of the
    state layer: a missing or malformed value reads as *off*, so a corrupt flag
    can never silently mute the assistant.
    """
    return store.get_bool(VACATION_ACTIVE_KEY, False)


def vacation_status(store: Any) -> dict[str, Any]:
    """The user's vacation state as a JSON-able dict (for CLI / ``GET /vacation``).

    ``{"active": bool, "since": str | None, "source": str | None}``. ``since`` and
    ``source`` are only meaningful while ``active`` is true; they read as ``None``
    when off (the keys are cleared on deactivate, so "off now" and "never on" look
    identical).
    """
    active = is_on_vacation(store)
    return {
        "active": active,
        "since": store.get_state(VACATION_SINCE_KEY) if active else None,
        "source": store.get_state(VACATION_SOURCE_KEY) if active else None,
    }


def activate(
    store: Any, *, now: datetime, source: str = SOURCE_MANUAL
) -> dict[str, Any]:
    """Turn vacation mode on; return the fresh :func:`vacation_status`.

    Idempotent on the *start time*: re-activating an already-on vacation refreshes
    the source but keeps the original ``since``, so "eased since Tue" doesn't reset
    to now on a second toggle. ``now`` is threaded in (no clock read here) and
    stored in :data:`~prefrontal.clock.TS_FMT`.
    """
    already_on = is_on_vacation(store)
    store.set_state(VACATION_ACTIVE_KEY, "1", source="explicit")
    store.set_state(VACATION_SOURCE_KEY, source, source="explicit")
    if not already_on:
        store.set_state(VACATION_SINCE_KEY, now.strftime(TS_FMT), source="explicit")
    return vacation_status(store)


def deactivate(store: Any) -> dict[str, Any]:
    """Turn vacation mode off and clear its state; return the fresh status.

    Clears all three keys rather than writing ``"0"`` so "off now" and "never on"
    are indistinguishable — re-entry is a clean slate, no stale ``since``/``source``
    left to leak into a later display (commandment 4, "design for the return").
    A no-op when already off.
    """
    store.delete_state(VACATION_ACTIVE_KEY)
    store.delete_state(VACATION_SINCE_KEY)
    store.delete_state(VACATION_SOURCE_KEY)
    return vacation_status(store)


def resume_on_return(store: Any) -> bool:
    """Auto-lift vacation mode on a return-home edge; return whether it did.

    Wired into the closed-loop trip state machine
    (:func:`prefrontal.trips.process_location`): when a trip closes because the
    phone is back inside the home radius, an active vacation is cleared. Returning
    home is the high-confidence "vacation's over" cue the design leans on for the
    safety-critical *exit* — so the user is never left muted for weeks because they
    forgot to toggle it back. Returns ``True`` iff a vacation was actually lifted
    (so the caller can surface a "welcome back" edge), ``False`` when none was on.
    """
    if not is_on_vacation(store):
        return False
    deactivate(store)
    return True


def suggest_threshold_minutes(store: Any) -> float:
    """Away-dwell (minutes) past which the entry suggestion fires, per user.

    Reads :data:`VACATION_SUGGEST_AFTER_KEY` (in nights) and floors it at one
    night, so a hand-edited ``0`` can't turn the suggestion into a same-day
    false-positive on an ordinary outing.
    """
    nights = store.get_float(VACATION_SUGGEST_AFTER_KEY, DEFAULT_SUGGEST_AFTER_NIGHTS)
    return max(1.0, nights) * 1440.0


def should_suggest_vacation(store: Any, *, away_minutes: float, already_asked: bool) -> bool:
    """Whether to raise the one-tap "away a while — ease off?" entry suggestion.

    Pure and total (no clock/store-mutation): the caller threads in how long the
    current trip has been open (``away_minutes``, e.g. an active trip's
    ``elapsed_minutes``) and whether this absence was already asked about
    (``already_asked`` — the engine's fire-once guard, so a single ask per trip).
    Says yes only when vacation isn't already on, the dwell has passed
    :func:`suggest_threshold_minutes`, and it hasn't asked yet. Deliberately a
    *suggestion* gate, never an auto-switch — a work offsite shouldn't silently
    mute the assistant; the human taps to confirm.
    """
    if is_on_vacation(store):
        return False
    if already_asked:
        return False
    return away_minutes >= suggest_threshold_minutes(store)


def vacation_suggestion_text(days: int) -> str:
    """The one-tap entry-suggestion copy, given whole days away so far."""
    return (
        f"You've been away for {days} days. Want me to ease off the non-urgent "
        "nudges until you're back? Tap 🏝️ for vacation mode."
    )
