- **Delegation send: a retried confirm can no longer deliver a duplicate** ✅ — the
  autonomous email-send path (`prefrontal.delegation.send_prepared_draft`) treated an
  already-sent draft as a non-blocking *warning*, while the send gate itself refused
  only on blockers and a stale content digest. A client that retried the confirm
  after a send that succeeded but timed out client-side (same, unchanged draft →
  same digest) sailed through the gate and sent a second copy — exactly the "single
  wrong autonomous send" the two-phase gate exists to prevent. An already-sent draft
  is now a hard refusal (`code="already_sent"`, surfaced as HTTP 409 on
  `POST /todos/{id}/delegate/send`), keyed off the durable `forwarded` +
  "sent draft to …" trace that a first send leaves (and that re-delegating clears).
  A new `allow_resend=True` argument covers a deliberate re-send (the first copy
  never arrived). Covered by new core + HTTP cases in `tests/test_delegation_send.py`.
