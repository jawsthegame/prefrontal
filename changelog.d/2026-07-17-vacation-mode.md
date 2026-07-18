- **Vacation mode** ✅ — ease off the nudges while you're away, without a
  prospective-memory tax. A **receptivity profile, not a kill switch**: while it's
  on the coaching tick holds every *discretionary* cue (the same non-`critical`,
  non-user-initiated set quiet hours holds), so a flight on the calendar and
  on-demand surfaces (panic, emotion support) still get through — vacation ≠ off.
  Resolves the "automatic-by-location vs. manual toggle" question toward
  **detect-and-confirm** (see [`docs/design/vacation-mode.md`](docs/design/vacation-mode.md)):
  **manual** entry is the escape hatch (`prefrontal vacation on|off|status`,
  `GET`/`POST /vacation`) for staycations location can't see, and returning inside
  the home radius **auto-resumes** it — the safety-critical half, since a
  *forgotten* manual off is what turns the tool into a permanent mute. The mode is
  a pure state core (`prefrontal/vacation.py`) over per-user `coaching_state` (no
  new schema); it threads `on_vacation` onto the coaching context and gates in
  `coaching.suppressed`, and the trip state machine (`trips.process_location`)
  lifts it on a return edge, echoing `vacation_resumed` out of
  `POST /webhooks/location`. Auto-*entry* suggestion (a one-tap confirm on
  away-dwell) and the visible banner are the tracked follow-up. Covered by
  `tests/test_vacation.py`.
