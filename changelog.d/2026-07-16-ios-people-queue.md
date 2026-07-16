- **iOS: people review queue** ✅ — the native app now surfaces the name-mention
  review queue (`GET /people/queue`), previously reachable only over HTTP. A
  **People to identify** row on the **Me** tab opens the queue of names that
  ingested items used but that aren't on the roster yet; each can be
  **Identified** — a sheet
  categorizes it into a new roster person with a relationship (mirroring the
  server's `RELATIONSHIPS`) and an importance on the shared 0–3 priority scale
  (`POST /people/mentions/{id}/identify`) — or **Dismissed**
  (`POST /people/mentions/{id}/dismiss`). The resulting roster feeds the behavioral
  profile and todo prioritization. v1 covers the queue workflow (identify-as-new +
  dismiss); linking a mention to an existing roster person and browsing/editing the
  roster are a follow-up. New `PeopleQueueView.swift`, a `PersonMention` model, and
  `APIClient.peopleQueue()` / `.identifyMention(_:relationship:importance:)` /
  `.dismissMention(_:)`, covered by `APIClientTests`.
