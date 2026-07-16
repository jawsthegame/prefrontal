- **Queryable behavioral model for commitments** ✅ — extends the `todo_events`
  continuity pattern to the calendar. A new append-only `commitment_events` table
  (`schema.sql`) records a `rescheduled` event whenever `upsert_commitment` sees a
  synced event's `start_at` move — the first sync establishes the time (not a
  reschedule), and an unchanged re-sync records nothing, so a calendar that polls
  constantly doesn't accrue phantom events. `prefrontal/memory/behavioral.py` reads
  that history into a deterministic `CommitmentBehavior` (reschedule count, recency,
  and a second-person context line — "This has been rescheduled three times, most
  recently 2 days ago. It keeps moving — worth confirming the time still holds"),
  plus a `commitment_nudge_clause` for a future departure/prep-reminder fold-in.
  Retrieved on demand via `GET /commitments/{id}/behavior` (the calendar twin of
  `GET /todos/{id}/behavior`). This is the continuity a plain calendar mirror throws
  away by overwriting `start_at` in place — "this appointment keeps shifting" is now
  answerable. `ScheduleRepo` gains `record_commitment_event` / `commitment_events` /
  `count_commitment_events`. Covered by `tests/test_commitments.py`.
