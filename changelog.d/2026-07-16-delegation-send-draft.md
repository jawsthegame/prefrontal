- **M4: send a prepared delegation draft ("drafts → does")** ✅ — the first
  *executing* action of roadmap M4 ("It does the thing, not just reminds",
  `docs/roadmap-vision.md` §4). Delegation's `agent` handler *prepares* email drafts
  but never sends them — they sit on the todo to copy out by hand. This promotes a
  prepared **email** draft to an actually-sent message over the user's existing SMTP
  source, behind a deliberate two-phase gate that mirrors the NL-assistant's
  propose→apply contract: `POST /todos/{id}/delegate/send/preview` shows *exactly*
  what would go out (recipient, subject, body, outbox) plus a content `digest`, and
  refuses up front on any blocker (no email draft, no valid recipient, unfilled
  `[bracketed placeholders]`, SMTP unconfigured); `POST /todos/{id}/delegate/send`
  re-resolves from the *current* stored draft and refuses a **stale** send (the
  content changed since preview — 409) or a structural blocker (422), then sends.
  Subject/body always come from the stored draft (never the caller), so a caller can
  pin *which* message goes out (via the digest) and *who* it goes to (a validated
  recipient), but can't inject content — the guard against the one autonomous-send
  mistake that would cost the trust the product runs on. A rejected transport send
  returns `sent: false` with the reason and keeps the draft (delegation marked
  `failed`), matching the email handler's "report, never raise" ethos; a success
  marks it `forwarded` with a "sent draft to …" detail as the auditable trace.
  Core in `prefrontal/delegation.py` (`preview_send` / `send_prepared_draft`, reusing
  the `SmtpClient` transport and `resolve_smtp_for` outbox pick); scoped + ownership-
  checked like every write. Deliberately a bounded, verifiable, confirmed action —
  never a browser agent. Covered by `tests/test_delegation_send.py` (preview blockers,
  the send happy path via a fake transport, stale-digest refusal, blocked refusal,
  transport-failure handling, already-sent warning, and the HTTP surface incl. auth,
  404, the full preview→send flow, 409, and 422).
