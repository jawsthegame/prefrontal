# n8n dependency audit

_Last updated: 2026-07-12._

Prefrontal is mid-migration off n8n: the long-run goal is that n8n owns only the
things it's genuinely good at (pulling external data *in* through its connectors),
while every "on a schedule, poll a Prefrontal endpoint, republish to ntfy"
workflow moves to a native launchd timer over the `prefrontal` CLI — one fewer
service, one delivery path, and no config that can drift out of sync. This
document is the running inventory of what still touches n8n and where each path
stands.

## TL;DR — what still *requires* n8n

Only the **triage** pair, and both are n8n doing its actual job (a connector):

| Path | Direction | Why it stays |
|---|---|---|
| `deploy/n8n/triage-ingest.workflow.json` | inbound | Pulls arbitrary external signals (Signal, feeds, a Gmail node) into `POST /triage`. Email specifically has a native path (`prefrontal mail fetch`), but generic third-party ingestion is what n8n's connectors are for. |
| `deploy/n8n/triage-urgent.workflow.json` | outbound | Delivers the `triage.urgent` event `triage.apply` POSTs to `N8N_WEBHOOK_URL` (`prefrontal/triage.py`) for imminent/overdue signals. No native delivery of urgent-triage pushes yet. |

Everything else has a native launchd replacement (see below). The remaining
workflow templates are kept only so an existing n8n install keeps working during
cutover; deactivate each once its native twin is confirmed.

## Migrated — native launchd replacement exists

Running both the workflow **and** its native twin double-sends, so deactivate the
n8n workflow (toggle Active off) once the launchd agent is confirmed.

| n8n workflow | Native replacement |
|---|---|
| `coach-check` | coaching tick — `coach --deliver` (`com.prefrontal-coach`) |
| `hyperfocus-check` | coaching tick (hyperfocus module) |
| `departure-reminder` | coaching tick (time-blindness module) |
| `panic-check` | coaching tick — `_deliver_panic` on the same tick |
| `coffee-shop-nudge` | coaching tick (location-anchor) + native `TwilioVoiceClient` for the 150% call |
| `encouragement` | coaching tick — `encouragement_cues` folded into `run_coaching_tick` |
| `focus-arm-check` | `coach.sh` runs `prefrontal focus arm --all-users` (shared `arm_focus_session`) |
| `morning-briefing` | `prefrontal briefing --deliver --all-users` (`com.prefrontal-briefing`, daily 7am) |
| `chores-check` / `checkin-check` / `digest-check` / `star-prompt-check` | household sweeps (`com.prefrontal-household`) |
| `usage-check` | `prefrontal usage check --deliver` (`com.prefrontal-usage`) |
| `calendar-sync` (workflow already removed) | `prefrontal calendar sync` (`com.prefrontal-calendar`) |

Endpoints and their native tick share one decision function each, so the HTTP and
launchd paths can't drift: `run_coaching_tick`, `evaluate_panic_check`,
`arm_focus_session`, `run_usage_check`.

## Code-level n8n surface (infrastructure, stays until n8n is fully retired)

- **`prefrontal/integrations/n8n.py`** — `N8nClient` (outbound event trigger) and
  `N8nWorkflowSyncer` (pushes `deploy/n8n/*.json` into a running n8n over its REST
  API). Both no-op cleanly when unconfigured (local-first).
- **Outbound triggers** (fire-and-forget, no-op if `N8N_WEBHOOK_URL` unset):
  - `n8n.trigger("triage.urgent", …)` — `prefrontal/triage.py` (the one path that
    still *needs* n8n to actually deliver; see above).
  - `n8n.trigger("episode.logged", …)` — `prefrontal/webhooks/routers/ingestion.py`,
    on every one-tap Shortcut episode. Optional; nothing depends on it landing.
- **Inbound endpoint** — `POST /webhooks/n8n` (`ingestion.py`) normalizes any
  pushed event into a `Signal` and runs it through triage. Named for n8n but
  generic: any client can POST to it, so it's not a hard dependency.
- **Workflow sync CLI** — `prefrontal n8n push`, wired into `deploy/update.sh` so
  the Update button also syncs workflow definitions. See `docs/n8n-sync.md`.
- **Config** — `N8N_WEBHOOK_URL` / `N8N_WEBHOOK_TOKEN` / `N8N_API_URL` /
  `N8N_API_KEY` (`prefrontal/config.py`, `.env.example`). All optional.

## Remaining work to fully retire n8n

1. A native delivery path for **urgent triage** (so `triage-urgent` can go), and
2. a first-class **ingestion** story for non-mail external sources (so
   `triage-ingest` can go) — or an accepted decision that n8n stays as the
   inbound connector.

Once those land, the `N8nClient`/`N8nWorkflowSyncer`, the config keys, and
`deploy/n8n/` can all be removed.
