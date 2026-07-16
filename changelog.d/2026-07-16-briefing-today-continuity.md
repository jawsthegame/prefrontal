- **Reschedule continuity in the briefing's "📅 Today" section** ✅ — the last
  proactive surface not yet carrying commitment continuity. Each of today's
  schedule lines now folds in a compact `· moved N×` suffix from the commitment's
  `commitment_events` history — "18:00 — Dentist · Cal **· moved 2×**" — so a
  calendar item that keeps shifting reads as such at a glance, the schedule twin of
  the avoidance surfaces' `· rescheduled N×` and the departure reminder's "moved 3×
  on the calendar". Added `commitment_digest_suffix(store, commitment_id)` to
  `prefrontal/memory/behavioral.py` (the commitment analogue of
  `behavior_digest_suffix`, counts-only, leading-` · ` so it appends cleanly);
  `build_briefing` tags each `today` commitment with a `continuity` key and the
  renderer appends it. Every shown item is queried (all of today's commitments
  render), so there's no over-computation. Covered by `tests/test_briefing.py` and
  `tests/test_commitments.py`.
