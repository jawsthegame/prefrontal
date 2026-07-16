- **Reschedule continuity in the departure reminder** ✅ — completes the
  commitment-continuity wiring (the calendar twin of the todo nudge fold-in): the
  `commitment_nudge_clause` helper shipped with the commitment behavioral model but
  wasn't yet in a delivered reminder. Now `build_departure_message` takes a
  `continuity` clause, folded into the same trailing slot as the note (caution
  first, then the packing note), and both delivered departure paths —
  `evaluate_departure_check` (`/webhooks/departure/check`) and the Time Blindness
  `departure_buffer` cue — pass it: "Hey Tom, leave now for the dentist (~15 min
  travel). **Heads up — this has moved 3× on the calendar.** Note: bring the
  insurance card." So a "leave now" for an event that keeps shifting flags that it
  might move again, rather than treating the latest synced time as settled. Empty
  history appends nothing (the same leading-space contract as `note_hint`). Covered
  by `tests/test_departure.py`.
