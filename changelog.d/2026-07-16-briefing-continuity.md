- **Reschedule/snooze continuity in the morning briefing** ✅ — the briefing's two
  avoidance surfaces (🐢 Keeps sliding, 🧭 Time to decide) named an item and its age
  but not *how* you'd been handling it. They now fold in a compact continuity suffix
  via the new `behavior_digest_suffix(store, todo_id)`: "File the reimbursement —
  open 9 days **· rescheduled 1×, snoozed 1×**", and "Renew passport — 25d **·
  rescheduled 2×**; break it down, defer, or drop it". It's the digest-sized slice of
  the queryable behavioral model (`prefrontal/memory/behavioral.py`) — `×N` counts
  rather than the nudge path's full sentence — so the "time to decide" framing reads
  as *you've actively moved this*, not merely that it's old. Counts-only and
  append-anywhere (empty history → no suffix), matching the `behavior_nudge_clause`
  contract. Both `Briefing.avoided` and `Briefing.checkpoint` items now carry a
  `continuity` key. Covered by `tests/test_stuck_checkpoint.py` and
  `tests/test_behavioral.py`.
