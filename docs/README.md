# Prefrontal docs

Prefrontal is a local-first executive-function assistant — it tracks your
outings, calendar, todos, focus, and mail, learns your patterns, and nudges you
at the right moment, all on your own machine. It's multi-tenant, and includes a
shared **household sheet / Parent pack** for co-parents.

**New here? Start with the [Usage Guide](guide.md)** — a tour of every
capability with real examples, plus an API/CLI reference.

## Map

**Use it**
- [**guide.md**](guide.md) — capabilities tour, real-world examples, API & CLI reference. *Start here.*
- [**deployment.md**](deployment.md) — set it up on a Mac mini: Python, launchd, Ollama, n8n, Tailscale, iOS Shortcuts.
- [**n8n-sync.md**](n8n-sync.md) — push `deploy/n8n/*.json` into the running n8n on update (the Update button updates n8n too).
- [**multi-tenant.md**](multi-tenant.md) — users, tokens, per-user scoping, and the `/admin` + `prefrontal user` surfaces.

**Understand it**
- [**whitepaper.md**](whitepaper.md) — the problem, the design principles, and the architecture in one read.
- [**schema.md**](schema.md) — the SQLite memory model (episodes, patterns, commitments, todos, household tables, …).
- [**entity-relationship.md**](entity-relationship.md) — the ERD: every table and its foreign-key relationships, as a Mermaid diagram.
- [**interventions.md**](interventions.md) — every module's interventions (what each does, its trigger, its status).
- [`../ROADMAP.md`](../ROADMAP.md) — what's shipped and what's planned.

**Design specs** (the "why" behind individual pieces)
- [**household-sheet.md**](household-sheet.md) — the shared co-parent sheet / Parent-pack backbone.
- [**coaching-agent.md**](coaching-agent.md) — generating reminders/check-ins from the profile.
- [**triage-agent.md**](triage-agent.md) — classifying and routing inbound signals.
- [**impulsivity.md**](impulsivity.md) — the reflective-pause / capture-then-defer module.
- [**encouragement.md**](encouragement.md) — the rough-day reassurance & recovery layer.

**Reader-facing**
- [**one-sheet.pdf**](one-sheet.pdf) · [**parent-pack.pdf**](parent-pack.pdf) — the visual one-sheets.
- [**brand/**](brand/) — the Prefrontal mark, lockup, and palette.

## At a glance

| Capability | Surfaced via | Guide |
|---|---|---|
| Outing nudges (coffee-shop) | iOS Shortcut + n8n | [↗](guide.md#outings-the-coffee-shop-nudge) |
| Departure reminders | n8n + location | [↗](guide.md#departure-reminders) |
| Calendar sync & conflicts | per-user private ICS feeds (native) | [↗](guide.md#calendar-sync) |
| Todos: augment / decompose / fit / avoid | dashboard, CLI, API | [↗](guide.md#todos) |
| Focus sessions | iOS Shortcut + n8n | [↗](guide.md#focus-sessions) |
| Morning briefing | n8n (7am) | [↗](guide.md#morning-briefing) |
| Profile & learning | nightly `learn`/`summarize` | [↗](guide.md#behavioral-profile--learning) |
| Mail ingestion & triage | n8n / built-in IMAP | [↗](guide.md#mail-ingestion--triage) |
| Self-care (meal/water) checks | opt-in `self_care` module | [↗](guide.md#modules) |
| Shared household sheet / Parent pack | `/kids` dashboard + `prefrontal household` | [household-sheet.md](household-sheet.md) |
| Dashboard · family view · widget | web + Tailscale | [↗](guide.md#surfaces-dashboard-family-view--widget) |
