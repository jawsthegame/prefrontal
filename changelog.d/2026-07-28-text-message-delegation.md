- **Delegation: text a person a question (`sms` handler)** ✅ — extends the delegate
  feature past the AI-prep (`agent`) and human-VA (`email`) handlers with a third,
  `sms`: the "just ask someone" case where a todo is blocked on a quick answer from
  someone in your life ("Dad wants help on the 19th — nothing on the calendar, just
  triple-check with my wife we're free"). Instead of a research brief, the local
  model drafts *one* short, natural question (`generate_question`, with an offline
  heuristic that leans on your own phrasing) and it's texted to a phone-number
  `destination` over the operator's Twilio account (the same one household invites
  use), via a new `TwilioConfig` resolved from `Settings`. Mirrors the `email`
  handler's contract exactly — ends `forwarded` on send and parks for the "heard
  back?" check-in (now worded for a text, not a VA hand-off), and a bad number /
  unconfigured Twilio / transport error stores the drafted text and ends `failed` so
  nothing is lost. Wired through the whole delegate surface: `POST /todos/{id}/delegate`
  (`handler:"sms"`, requires a phone `destination`), the NL assistant's `delegate_todo`
  op ("text my wife to check if we're free the 19th"), `prefrontal todo delegate
  --handler sms --to <number>`, and a "💬 Text someone…" option in the dashboard's
  delegate popover with a recent-numbers pick-list (`/todos/delegate-recipients` now
  returns email and sms recipients separately). One-way for now — the reply lands on
  the Twilio number, so you `mark returned` by hand; an inbound SMS loop-closer (the
  mirror of `match_delegated_reply`) is future work.
