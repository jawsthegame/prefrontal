- **Mail triage: todo status + "Create todo" offer (iOS + server)** ✅ — the Mail
  tab now shows, for each triaged message, whether it's tracked as a todo, and
  offers to create one where it isn't. The "Needs action" rows carry a green
  **Todo** chip confirming the open loop; a new **Needs a todo** section lists
  messages triage flagged as needing action but whose todo was *suppressed* at
  ingest (a no-reply/notification sender, an informational category, or a learned
  repeat-dropped sender), each with a one-tap **Create todo** button. Creating one
  moves the message into "Needs action" on the next load. Server: `GET /mail` now
  also returns `needs_action_no_todo` (needs-action rows with a NULL `todo_id`,
  kept out of the clean action list), and a new `POST /mail/{id}/todo` creates a
  todo whose title/notes/priority match what ingest would have produced and links
  it back (`mail_messages.todo_id`) — idempotent against a double-tap. New repo
  methods `mail_needing_action_no_todo`, `get_mail`, and `link_mail_todo` (which
  never clobbers an existing link). iOS: `MailInbox` decodes the new list
  tolerantly (an older server without it → empty), plus `createMailTodo` on the
  API client. Covered by `tests/test_mail.py` and `ios/PrefrontalTests/APIClientTests.swift`.
