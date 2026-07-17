- **People queue: a dismissed name stays dismissed** ✅ — dismissing a person-mention
  ("not a person") marks the row ``dismissed``, but the queue's de-dupe is a *partial*
  unique index over ``status = 'pending'`` only, and a dismissed mention leaves no
  ``people`` row for `enqueue_mentions` to short-circuit on. So, before this fix, the
  next email or calendar item naming the same phrase re-queued an identical pending
  mention — a recurring false positive ("Order Confirmation", "Field Trip") had to be
  re-dismissed forever and the review queue never converged. `enqueue_mentions` now
  consults a new `MemoryStore.has_dismissed_mention(name_key)` (backed by a partial
  index on the dismissed rows) and skips any name the user has already dismissed
  (identifying it instead still creates a ``people`` row, the path already handled).
  Covered by a new case in `tests/test_people.py`.
