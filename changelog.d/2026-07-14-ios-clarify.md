- **iOS: ambiguity clarification** ✅ — the native app now surfaces the
  task-paralysis module's task-initiation lever. A new **Clarify** screen (reached
  from the **Todos** tab — a toolbar button plus a banner when the queue is
  non-empty) lists the vague todos/commitments the server flagged with a pending
  question (`GET /clarifications`); answering one — tap an offered reading or type
  your own (`POST /clarifications/{id}/resolve`) — hones it into something
  startable, and when the chosen reading maps to a built-in **playbook** the
  guided walkthrough opens in a sheet. Also: a **"check now"** sweep button
  (`POST /clarifications/check`), a **dismiss** ("not ambiguous") action, and a
  **Recently honed** list that re-opens a resolved item's guide
  (`/clarifications/playbooks/{task_type}`). New `ClarifyView.swift`,
  `Clarification`/`Playbook` models, and the matching `APIClient` methods.
