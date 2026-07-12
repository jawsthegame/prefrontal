# deploy/

Glue files for running Prefrontal on an always-on Mac mini. Follow the full
runbook in [`../docs/deployment.md`](../docs/deployment.md); these are the files
it tells you to copy/import.

| File | What it is | Used in |
|---|---|---|
| `com.prefrontal.plist` | launchd agent that runs `prefrontal serve` always-on (edit the paths). | deployment §3 |
| `learn.sh` | Chains the nightly learning pass: `prefrontal learn` then `prefrontal summarize`, with timestamped logging. | deployment §12 |
| `com.prefrontal-learn.plist` | launchd agent that runs `learn.sh` nightly at 03:30 (periodic, no `KeepAlive`). | deployment §12 |
| `com.prefrontal-mail.plist` | launchd agent that runs `mail-fetch.sh` every 15 min to fetch + triage mail for all users (the no-n8n path). Each user connects mailboxes via `prefrontal mail add-source`. | deployment §13 |
| `mail-fetch.sh` | Wrapper the mail agent calls: runs `prefrontal mail fetch --all-users`, from the repo root so `.env` loads. | deployment §13 |
| `coach.sh` | Wrapper the coach agent calls: runs `prefrontal coach --deliver` (fans over every enabled module + the overwhelm check), from the repo root so `.env` loads. | deployment §19 |
| `com.prefrontal-coach.plist` | launchd agent that runs `coach.sh` every 60s — the **native replacement** for the `coach-check`, `hyperfocus-check`, `departure-reminder`, and `panic-check` n8n workflows (deactivate those before enabling). | deployment §19 |
| `household-sweeps.sh` | Wrapper: runs the shared-household checks (`household chores-check` / `checkin-check` / `digest-check` / `prompt-check`), each self-gating on due-ness. | deployment §19 |
| `com.prefrontal-household.plist` | launchd agent that runs `household-sweeps.sh` every 15 min — the **native replacement** for the `chores-check`, `checkin-check`, `digest-check`, and `star-prompt-check` n8n workflows (deactivate those before enabling). | deployment §19 |
| `usage-nudge.sh` | Wrapper: runs `prefrontal usage check --deliver --all-users` — the weekly feature-usage nudge (mute-or-keep a firing-but-ignored module), self-gating to once per ISO week per user. | deployment §19 |
| `com.prefrontal-usage.plist` | launchd agent that runs `usage-nudge.sh` hourly — the **native replacement** for the `usage-check` n8n workflow (deactivate it before enabling). | deployment §19 |
| `calendar-sync.sh` | Wrapper the calendar agent calls: runs `prefrontal calendar sync --all-users` (fetch + parse each user's private ICS feeds into `commitments`), from the repo root so `.env` loads. | deployment §10 |
| `com.prefrontal-calendar.plist` | launchd agent that runs `calendar-sync.sh` every 15 min — the **native replacement** for the `calendar-sync` n8n workflow (per-user ICS feeds via `prefrontal calendar add-source`; deactivate the n8n workflow before enabling). | deployment §10 |
| `package.sh` | Re-packages the built artifacts after a pull: a Python wheel + sdist (`dist/`), the client handout PDFs rendered from their HTML (`dist/handouts/`), and the macOS desktop app (`desktop/dist/`). **Best-effort and non-fatal** — each step self-skips when its toolchain/source is absent (no `build` module, no Chrome, no `npm`) and only writes gitignored dirs, so it never dirties the checkout or blocks a restart. Run explicitly (`bash deploy/package.sh`) or in CI — **not** by `update.sh`, whose fast pull+restart must not wait on the multi-minute desktop build. | deployment §12 |
| `n8n/departure-reminder.workflow.json` | Importable n8n workflow: schedule → `POST /webhooks/departure/check` → **ntfy** (with the signed **Made it / Missed it** buttons the endpoint returns) → `POST /webhooks/n8n`. A template — adjust nodes for your n8n version. **Superseded by `coach.sh`** (native). | deployment §7 |
| `n8n/coffee-shop-nudge.workflow.json` | Location-Aware Task Anchor: every-minute poll of `/webhooks/outing/check` → **ntfy at 50%/100%** (with **I'm back / Abandon** buttons) → **Twilio voice call at 150%**. | deployment §8 |
| `n8n/hyperfocus-check.workflow.json` | Hyperfocus: every-minute poll of `/webhooks/focus/check` → **ntfy** on a due interrupt (gentle alignment **check**, or a higher-priority biological **break**). Activates the passive interrupts; the reflective-pause + capture flows are interactive and need no workflow. The push carries the endpoint's signed **Wrap up** action button (background `GET /nudge/act` — no app switch, nothing to name-match). **Superseded by `coach.sh`** (native). | deployment §9 |
| `n8n/morning-briefing.workflow.json` | Daily 7am: `GET /briefing` → **ntfy** the digest (today, conflicts, slips, coaching note). | deployment §11 |
| `n8n/panic-check.workflow.json` | Proactive panic mode: every-20-min poll of `/webhooks/panic/check` → **ntfy** **only when the plate tips into overwhelm** (server edge-triggers + cooldown, so it nudges once per spike, not every poll). **Superseded by `coach.sh`** (native — panic runs on the same tick). | deployment §11 |
| `n8n/coach-check.workflow.json` | **Unified coaching tick**: polls `/webhooks/coach/check` (the agent that fans over every enabled module), then publishes each returned cue to ntfy at a priority matching the channel the agent chose (digest→low … voice→max), passing through any one-tap `actions` a cue carries (e.g. the Self-Care meal check's **Ate** / **Snooze**). **Superseded by `coach.sh`** — the same tick, run natively instead of polled over HTTP. | deployment §15 |
| `n8n/encouragement.workflow.json` | **Rough-day recovery** (opt-in): polls `GET /encouragement` a few times an afternoon and, on a genuinely rough & unsent day, publishes the recovery message to ntfy **once**, then `POST /encouragement/sent` to stamp the day. Off unless the `encouragement` coaching key is `on`. | deployment §16 |
| `n8n/focus-arm-check.workflow.json` | Every-few-minutes poll of `/webhooks/focus/arm` — auto-starts a focus session from a *live* calendar focus block (zero taps). | deployment §17 |
| `n8n/triage-ingest.workflow.json` | Gmail/IMAP intake → `POST /triage`: classify + prioritize incoming mail into action items. | deployment §17a |
| `n8n/triage-urgent.workflow.json` | Webhook (`prefrontal-events`) → surfaces an urgent triaged item as a push. | deployment §17a |
| `n8n/chores-check.workflow.json` | Household: poll `/webhooks/household/chores/check` → owner reminder + slip heads-up. **Superseded by `household-sweeps.sh`** (native). | deployment §18 |
| `n8n/checkin-check.workflow.json` | Household: poll `/webhooks/household/checkin/check` → weekly mental-load check-in. **Superseded by `household-sweeps.sh`** (native). | deployment §18 |
| `n8n/digest-check.workflow.json` | Household: poll `/webhooks/household/digest/check` → the other parent's unseen sheet changes. **Superseded by `household-sweeps.sh`** (native). | deployment §18 |
| `n8n/star-prompt-check.workflow.json` | Household: poll `/webhooks/household/star-prompts/check` → "did they earn a star today?" prompt. **Superseded by `household-sweeps.sh`** (native). | deployment §18 |
| `n8n/usage-check.workflow.json` | Feature-usage loop: hourly poll of `/webhooks/usage/check` → one push with a one-tap **Mute / Keep** when a coaching module keeps firing but you rarely act on it (self-gates to once/ISO-week per user). **Superseded by `usage-nudge.sh`** (native). | deployment §19 |
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
