# deploy/

Glue files for running Prefrontal on an always-on Mac mini. Follow the full
runbook in [`../docs/deployment.md`](../docs/deployment.md); these are the files
it tells you to copy/import.

| File | What it is | Used in |
|---|---|---|
| `com.morningstatic.prefrontal.plist` | launchd agent that runs `prefrontal serve` always-on (edit the paths). | deployment §3 |
| `n8n/departure-reminder.workflow.json` | Importable n8n workflow: schedule → `GET /profile` → Ollama → Pushover/Ntfy → `POST /webhooks/n8n`. A template — adjust nodes for your n8n version. | deployment §7 |
| `n8n/coffee-shop-nudge.workflow.json` | Location-Aware Task Anchor: every-minute poll of `/webhooks/outing/check` → Pushover at 50%/100% → **Twilio voice call at 150%**. | deployment §8 |
| `n8n/calendar-sync.workflow.json` | Syncs personal Google Calendar + a work ICS feed into `commitments` (merged, deduped) and fires a Pushover **double-booking alert** on overlaps. | deployment §10 |
| `n8n/morning-briefing.workflow.json` | Daily 7am: `GET /briefing` → Pushover the digest (today, conflicts, slips, coaching note). | deployment §11 |
| `ios-shortcut.md` | Recipes for "Made it"/"Missed it", the location automation, the "Going out"/"I'm back" outing shortcuts, and the "Getting into"/"Surfacing" focus-session shortcuts. | deployment §6 |

Everything here is safe to commit: the plist and workflow contain **placeholders**
(`REPLACE_WITH_...`, `PUSHOVER_TOKEN`) — no real secrets. Put real values in your
local `.env` and in n8n credentials, never in these files.
