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
#: Coaching-state key: what turned it on — ``"manual"`` or ``"auto"`` (a future
#: confirmed location suggestion). Recorded for explainability, not gating.
VACATION_SOURCE_KEY = "vacation_source"

#: How :func:`resume_on_return` records the auto-lift, so the source is legible
#: after the fact even though the state itself is cleared.
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"


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
