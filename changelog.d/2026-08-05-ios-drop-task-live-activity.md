- **iOS: Live Activities no longer track a bare started task** ✅ — the Lock
  Screen / Dynamic Island timer is now reserved for the two bounded,
  deliberately-begun sessions (**outing** and **focus**); starting a plain todo
  no longer spawns a Live Activity. The task kind (added in M2) counted *up* from
  the todo's `started_at`, but a todo with no estimate had no stale mark, so the
  timer never went stale and lingered in the Dynamic Island indefinitely until the
  todo was cleared. The priority ladder collapses to **outing → focus**;
  `SessionActivityAttributes` drops its `task` kind and `LiveActivityManager.sync`
  drops its `task:` parameter (and with it the now-unused `Todo.current(in:)`
  selector and `LiveActivityTaskTests`). Elapsed-time externalization for a
  started todo still lives in-app on the Todos "in progress" chip.
