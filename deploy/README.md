# deploy/

Glue files for running Prefrontal on an always-on Mac mini. Follow the full
runbook in [`../docs/deployment.md`](../docs/deployment.md); these are the files
it tells you to copy/import.

| File | What it is | Used in |
|---|---|---|
| `com.morningstatic.prefrontal.plist` | launchd agent that runs `prefrontal serve` always-on (edit the paths). | deployment §3 |
| `learn.sh` | Chains the nightly learning pass: `prefrontal learn` then `prefrontal summarize`, with timestamped logging. | deployment §12 |
| `com.morningstatic.prefrontal-learn.plist` | launchd agent that runs `learn.sh` nightly at 03:30 (periodic, no `KeepAlive`). | deployment §12 |
| `com.morningstatic.prefrontal-mail.plist` | launchd agent that runs `mail-fetch.sh` every 15 min to fetch + triage mail (the no-n8n path; edit paths + account list). | deployment §13 |
| `mail-fetch.sh` | Wrapper the mail agent calls: runs `prefrontal mail fetch` once per account, from the repo root so `.env` loads. | deployment §13 |
| `n8n/departure-reminder.workflow.json` | Importable n8n workflow: schedule → `GET /profile` → Ollama → Pushover/Ntfy → `POST /webhooks/n8n`. A template — adjust nodes for your n8n version. | deployment §7 |
| `n8n/coffee-shop-nudge.workflow.json` | Location-Aware Task Anchor: every-minute poll of `/webhooks/outing/check` → Pushover at 50%/100% → **Twilio voice call at 150%**. | deployment §8 |
| `n8n/hyperfocus-check.workflow.json` | Hyperfocus: every-minute poll of `/webhooks/focus/check` → Pushover on a due interrupt (gentle alignment **check**, or a higher-priority biological **break**). Activates the passive interrupts; the reflective-pause + capture flows are interactive and need no workflow. The Pushover push carries a **"Wrap up (End focus)"** action URL (`shortcuts://run-shortcut?name=End%20focus`) — ⚠️ this name **must exactly match your End-focus shortcut's name** (URL-encode spaces as `%20`), or the tap does nothing. Remember to toggle the workflow **Active** after import. | deployment §9 |
| `n8n/calendar-sync.workflow.json` | Syncs personal Google Calendar + a work ICS feed into `commitments` (merged, deduped) and fires a Pushover **double-booking alert** on overlaps. | deployment §10 |
| `n8n/morning-briefing.workflow.json` | Daily 7am: `GET /briefing` → Pushover the digest (today, conflicts, slips, coaching note). | deployment §11 |
| `n8n/panic-check.workflow.json` | Proactive panic mode: every-20-min poll of `/webhooks/panic/check` → Pushover **only when the plate tips into overwhelm** (server edge-triggers + cooldown, so it nudges once per spike, not every poll). | deployment §11 |
| `n8n/interactive-nudge-ntfy.workflow.json` | **One-tap ntfy action buttons**: polls `/webhooks/outing/check` and publishes to ntfy with the signed `actions` the endpoint returns (I'm back / Abandon), so a tap fires `GET /nudge/act` in the background — no app switch. Departure (Made it / Missed it) and focus (Wrap up) follow the same shape. | deployment §14 |
| `n8n/coach-check.workflow.json` | **Unified coaching tick**: polls `/webhooks/coach/check` (the agent that fans over every enabled module), then publishes each returned cue to ntfy at a priority matching the channel the agent chose (digest→low … voice→max). One workflow eventually replaces the per-module poll loops. | deployment §15 |
| `ios-shortcut.md` | Recipes for "Made it"/"Missed it", the location automation, the "Going out"/"I'm back" outing shortcuts, and the one-tap **"Panic"** shortcut. | deployment §6 |

Everything here is safe to commit: the plist and workflow contain **placeholders**
(`REPLACE_WITH_...`, `PUSHOVER_TOKEN`) — no real secrets. Put real values in your
local `.env` and in n8n credentials, never in these files.
