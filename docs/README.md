# Prefrontal docs

Prefrontal is a local-first executive-function assistant — it tracks your
outings, calendar, todos, focus, and mail, learns your patterns, and nudges you
at the right moment, all on your own machine.

**New here? Start with the [Usage Guide](guide.md)** — a tour of every
capability with real examples, plus an API/CLI reference.

## Map

**Use it**
- [**guide.md**](guide.md) — capabilities tour, real-world examples, API & CLI reference. *Start here.*
- [**deployment.md**](deployment.md) — set it up on a Mac mini: Python, launchd, Ollama, n8n, Tailscale, iOS Shortcuts.
- [**multi-tenant.md**](multi-tenant.md) — users, tokens, per-user scoping, and the `/admin` + `prefrontal user` surfaces.

**Understand it**
- [**schema.md**](schema.md) — the SQLite memory model (episodes, patterns, commitments, todos, …).
- [`../ROADMAP.md`](../ROADMAP.md) — what's shipped and what's planned.

**Design specs** (the "why" behind individual agents)
- [**coaching-agent.md**](coaching-agent.md) — generating reminders/check-ins from the profile.
- [**triage-agent.md**](triage-agent.md) — classifying and routing inbound signals.
- [**impulsivity.md**](impulsivity.md) — the reflective-pause / capture-then-defer module.
- [**encouragement.md**](encouragement.md) — the rough-day reassurance & recovery layer.

## At a glance

| Capability | Surfaced via | Guide |
|---|---|---|
| Outing nudges (coffee-shop) | iOS Shortcut + n8n | [↗](guide.md#outings-the-coffee-shop-nudge) |
| Departure reminders | n8n + location | [↗](guide.md#departure-reminders) |
| Calendar sync & conflicts | n8n ICS feeds | [↗](guide.md#calendar-sync) |
| Todos: augment / decompose / fit / avoid | dashboard, CLI, API | [↗](guide.md#todos) |
| Focus sessions | iOS Shortcut + n8n | [↗](guide.md#focus-sessions) |
| Morning briefing | n8n (7am) | [↗](guide.md#morning-briefing) |
| Profile & learning | nightly `learn`/`summarize` | [↗](guide.md#behavioral-profile--learning) |
| Mail ingestion & triage | n8n / IMAP | [↗](guide.md#mail-ingestion--triage) |
| Dashboard · family view · widget | web + Tailscale | [↗](guide.md#surfaces-dashboard-family-view--widget) |
