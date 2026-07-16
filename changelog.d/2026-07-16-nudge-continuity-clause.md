- **Behavioral continuity in delivered nudges** ✅ — the queryable behavioral model
  (`todo_events` + `prefrontal/memory/behavioral.py`) was retrievable on demand but
  didn't yet reach the nudges themselves. Now the two "you've been putting this off"
  reminders — Task Paralysis's `tiny_first_step` and Open Window's gap offer — fold
  in a compact reschedule/snooze continuity clause via the new
  `behavior_nudge_clause(store, todo_id)`: "You keep putting off *Call the
  accountant* (5d and counting). **You've rescheduled it twice.** Start tiny — …".
  It surfaces the dimension a plain days-open count can't — that you've *actively
  circled* this item, not just let it sit — which is the external-brain continuity a
  generic reminder can't offer. Deliberately a *compact* clause, not the full
  `context_lines` (age is already in the nudge; estimate bias would bury the ask),
  leading-space-prefixed like `note_hint` so an empty history appends cleanly, and
  counts-only so it stays cheap on the per-tick coaching path. A currently-snoozed
  todo is inert (not "avoided"), so the snooze count naturally shows on items whose
  park has lapsed and that keep coming back. Covered by `tests/test_behavioral.py`
  and `tests/test_coaching.py`.
