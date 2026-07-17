- **Mail: needs-action list is open loops, not the raw verdict** ✅ — the "Needs
  action" section of `GET /mail` (the iOS Mail tab, web dashboard, and CLI `mail
  list`) was flooding with automated noise — notifications, AWS/Datadog/monitoring
  alerts, and other mail that doesn't need a reply. Two fixes: (1)
  `MailRepo.mail_needing_action()` now returns only messages with an **open linked
  todo** — the genuine open loops — instead of every row still carrying a
  `needs_action` flag. Mail triaged as actionable but gated out of todo creation
  (no-reply/automated senders, informational categories) is still recorded and
  still shows in the `recent` feed; it just no longer clutters the action list.
  (2) `suppress_todo_reason` grows an `_AUTOMATED_SENDER_MARKERS` gate so alerting/
  monitoring senders (AWS `amazonaws.com`, Datadog, PagerDuty, OpsGenie,
  CloudWatch, `alert@`/`monitoring@`/bounce daemons) never spawn a todo even when
  the model flags them, mirrored into the down-model heuristic's `_BULK_MARKERS`.
  Because panic/next-thing score from the same list, an urgent-but-suppressed alert
  also stops registering as a "fire bearing down." Covered by `tests/test_mail.py`,
  with `tests/test_panic.py` / `tests/test_next_thing.py` updated to link the todo
  real ingest creates.
