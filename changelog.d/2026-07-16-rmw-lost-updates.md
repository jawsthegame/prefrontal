- **No lost updates when two requests edit the same aggregate at once** ✅ — a few
  writes read a JSON/CSV blob, mutate it in Python, and write the whole thing back.
  Under the per-thread WAL connections the webhook server uses, two concurrent
  same-user requests could both read the old value and the second write would
  clobber the first — a checked-off step, a mute toggle, or a saved availability day
  silently lost. `MemoryStore.transaction()` now wraps such a read-modify-write in a
  single `BEGIN IMMEDIATE` transaction (write lock taken *before* the read, so a
  concurrent writer waits on the existing 5s `busy_timeout` and then reads the
  updated value). Applied to `set_step_done` (todo decomposition progress),
  `set_feature_muted` (the Insights mute set), and the `/schedule/available-hours`
  partial-day merge. (`set_care_recipient_names` was checked and is a wholesale
  replace, not a read-modify-write, so it needs no change.) The wrapper is truly
  atomic — the `StateRepo` writes (`set_state`/`delete_state`) route their commit
  through `_commit`, so inside a block they join the transaction and roll back with
  it rather than settling on their own — and it refuses to nest (a clear error
  instead of an opaque SQLite one). Covered by new concurrency + transaction cases
  in `tests/test_store_concurrency.py`.
