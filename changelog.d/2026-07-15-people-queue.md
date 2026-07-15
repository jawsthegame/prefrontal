- **People queue: identify & categorize the names ingested items mention** ✅ —
  ingested items constantly *name* people ("call with Sam", "budget from Dana
  Ruiz", "Dr. Lee follow-up"), but those names were just text. Now the triage
  ingest path extracts them (deterministic, best-effort — never blocks capture) and
  drops any not-yet-known name into a **review queue**, where you identify it (link
  an existing person, or create one) and **categorize** it (a relationship —
  `family`/`coworker`/`friend`/`professional`/… — and an importance, `0`…`3`,
  mirroring todo priority). The resulting roster earns its keep on two axes the rest
  of the system already trades on: **learning** — the handful of importance-≥2 people
  become a "Key people" section in the summarizer profile injected into every agent,
  so a "call with Dana" is understood as *who* it concerns — and **prioritization** —
  a triage-created todo that names a high-importance person gets a small,
  capped priority bump (`people.priority_boost`), so an action about someone who
  matters edges up the list without inventing urgency. Nothing authoritative is
  written from a raw name: a mention stays `pending` (propose-don't-apply, like the
  LLM sensor and clarifications) until you resolve it, and a recurring name is kept
  to one queue row while quietly bumping the known person's recurrence count. New
  `people` + `person_mentions` tables (per-user, name-key normalized, alias-aware),
  a `PeopleRepo`, a `/people` router (`GET /people/queue`,
  `POST /people/mentions/{id}/identify|dismiss`, roster CRUD, and an on-demand
  `POST /people/extract` that can also use the model), and a
  `prefrontal people queue/identify/dismiss/list/add/categorize/scan` CLI. See
  `docs/schema.md`; covered by `tests/test_people.py`.
