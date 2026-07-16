- **Queryable behavioral model — entity-scoped continuity** ✅ — the profile
  summarizer serves one *population-level* snapshot (patterns + preferences,
  prose-summarized, prepended to every prompt); it can't answer a question scoped
  to *one* open loop. This adds the other half: a queryable behavioral model that,
  given a single todo, returns how the user has actually treated *that* item — so
  an agent can lead a reminder with "you've rescheduled this four times" instead of
  a cold nudge. The continuity a generic reminder app throws away by overwriting the
  deadline in place is now kept: a new append-only `todo_events` change log
  (`schema.sql`) records a `rescheduled` event whenever an existing deadline moves
  (the first-ever set isn't a reschedule) and a `deferred` event on each conscious
  snooze — wired into `update_todo_deadline` / `defer_todo` at the repo layer, so
  every call site logs for free. `prefrontal/memory/behavioral.py` reads that
  history into a deterministic, model-free `TodoBehavior`: reschedule/snooze counts,
  recency ("most recently 3 days ago"), age, the *task-specific* estimate bias
  (energy/category only — the global multiplier stays in `/profile`, not on every
  todo), and ready-to-inject second-person `context_lines` ordered most-actionable
  first (3+ reschedules escalates to a gentle "it keeps sliding" prompt). Retrieved
  on demand via `GET /todos/{id}/behavior` (JSON facts + context) or
  `prefrontal profile --todo <id>`. Covered by `tests/test_behavioral.py`.
