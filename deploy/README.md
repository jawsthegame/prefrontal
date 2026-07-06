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
| `n8n/departure-reminder.workflow.json` | Importable n8n workflow: schedule → `POST /webhooks/departure/check` → **ntfy** (with the signed **Made it / Missed it** buttons the endpoint returns) → `POST /webhooks/n8n`. A template — adjust nodes for your n8n version. | deployment §7 |
| `n8n/coffee-shop-nudge.workflow.json` | Location-Aware Task Anchor: every-minute poll of `/webhooks/outing/check` → **ntfy at 50%/100%** (with **I'm back / Abandon** buttons) → **Twilio voice call at 150%**. | deployment §8 |
| `n8n/hyperfocus-check.workflow.json` | Hyperfocus: every-minute poll of `/webhooks/focus/check` → **ntfy** on a due interrupt (gentle alignment **check**, or a higher-priority biological **break**). Activates the passive interrupts; the reflective-pause + capture flows are interactive and need no workflow. The push carries the endpoint's signed **Wrap up** action button (background `GET /nudge/act` — no app switch, nothing to name-match). Remember to toggle the workflow **Active** after import. | deployment §9 |
| `n8n/calendar-sync.workflow.json` | Syncs personal Google Calendar + a work ICS feed into `commitments` (merged, deduped) and fires an **ntfy double-booking alert** on overlaps. | deployment §10 |
| `n8n/morning-briefing.workflow.json` | Daily 7am: `GET /briefing` → **ntfy** the digest (today, conflicts, slips, coaching note). | deployment §11 |
| `n8n/panic-check.workflow.json` | Proactive panic mode: every-20-min poll of `/webhooks/panic/check` → **ntfy** **only when the plate tips into overwhelm** (server edge-triggers + cooldown, so it nudges once per spike, not every poll). | deployment §11 |
| `n8n/coach-check.workflow.json` | **Unified coaching tick**: polls `/webhooks/coach/check` (the agent that fans over every enabled module), then publishes each returned cue to ntfy at a priority matching the channel the agent chose (digest→low … voice→max), passing through any one-tap `actions` a cue carries (e.g. the Self-Care meal check's **Ate** / **Snooze**). One workflow eventually replaces the per-module poll loops. | deployment §15 |
| `n8n/encouragement.workflow.json` | **Rough-day recovery** (opt-in): polls `GET /encouragement` a few times an afternoon and, on a genuinely rough & unsent day, publishes the recovery message to ntfy **once**, then `POST /encouragement/sent` to stamp the day. Off unless the `encouragement` coaching key is `on`. | deployment §16 |
| `ios-shortcut.md` | Recipes for "Made it"/"Missed it", the location automation, the "Going out"/"I'm back" outing shortcuts, and the one-tap **"Panic"** shortcut. | deployment §6 |

**Updating the running n8n:** rather than re-importing each file by hand, set
`N8N_API_URL` + `N8N_API_KEY` in `.env` and `prefrontal n8n push` upserts every
workflow here into the live n8n over its REST API (matched by name, so re-runs
update in place — no duplicates). `update.sh` runs it, so the dashboard **Update**
button syncs workflows too; it's a clean no-op when the n8n API isn't configured.
See [`../docs/n8n-sync.md`](../docs/n8n-sync.md).

Delivery goes to **ntfy** by default — each publish node posts to `https://ntfy.sh`
on the `prefrontal-me` topic (change it, and point at a self-hosted ntfy if you
run one). Nudges carry the endpoint's signed one-tap **action buttons** so a tap
fires `GET /nudge/act` in the background — no app switch. (Pushover is still
supported by the native Python delivery client as a fallback; the n8n templates
just default to ntfy.)

Everything here is safe to commit: the plist and workflows contain **placeholders**
(`REPLACE_WITH_...`, the `prefrontal-me` topic) — no real secrets. Put real values
in your local `.env` and in n8n credentials, never in these files.
