- **Calendar sync no longer duplicates UID-less events on every poll** ✅ — a
  calendar event with no UID (a malformed but legal VEVENT) normalized to no
  ``external_id``, so `MemoryStore.upsert_commitment` couldn't match it on the next
  sync and INSERTed a fresh copy each poll — unbounded duplicates that also read as
  a hard double-booking of the event against itself in `find_conflicts`, and that
  `cancel_missing_calendar` never pruned. `sync_calendar` now assigns each id-less
  survivor a **stable synthetic id** (`_synthetic_external_id`, derived from the same
  title+start+end identity as the cross-feed dedupe key), so re-syncing the same
  event updates in place instead of duplicating. It's applied *after* `dedupe_events`
  so a real feed id is still preferred as the keeper for a mirrored event, and the id
  is deliberately un-namespaced so it can't cross-cancel another feed's events.
  Covered by a new case in `tests/test_commitments.py`.
