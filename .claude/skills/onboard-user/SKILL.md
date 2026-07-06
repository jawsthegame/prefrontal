---
name: onboard-user
description: >-
  Onboard a person onto a Prefrontal deployment end-to-end — provision their
  user (or mint a household invite code for a co-parent), then hand them a
  personalized setup sheet for ntfy notifications, iOS Shortcuts, and the
  Scriptable widget — and optionally connects their own email (IMAP) and calendar
  (private ICS feed) sources. Use when asked to "invite a user", "add someone",
  "onboard a co-parent", "set up a new phone/user", or "get <name> onto
  Prefrontal". Runs the `prefrontal user`/`household`/`mail`/`calendar` CLI and
  fills the new person's handle, token, base URL, and ntfy topic into the client
  instructions.
---

# Onboard a Prefrontal user

Take a new person from "not on the system" to "phone set up and receiving
nudges". Two things happen: **membership** (a user account, optionally joined to
a household) and **client setup** (ntfy, Shortcuts, Scriptable, filled in with
their real values).

The canonical setup docs are the source of truth — do **not** duplicate their
steps into the repo, cite them:

- `deploy/ios-shortcut.md` — every iOS Shortcut
- `deploy/scriptable/README.md` — the home/lock-screen widget
- `docs/deployment.md` (§"Configure once") — ntfy env vars + the token credential
- `docs/multi-tenant.md` — the per-user token / household model
- `docs/design/per-user-sources.md` — per-user email (IMAP) + calendar (ICS) sources

## Step 0 — figure out which flow this is

Ask only what you can't infer. You need:

1. **Base URL** of the deployment — the Mac mini's Tailscale name/IP, e.g.
   `http://mac-mini.tail-scale.ts.net:8000`. Check `.env`
   (`PREFRONTAL_BASE_URL` / any existing docs) before asking.
2. **New account or existing user?**
   - *Brand-new person* → provision a user (Step 1). Only an **operator** can do
     this; it prints a token shown **once**.
   - *Existing user who should join a household* → skip to the invite flow
     (Step 2). Redeeming requires they are already a user **with no household**.
3. **Solo or household?** A user needs **no** household to use every personal
   feature (todos, outings, panic, focus). Only add them to a household if they
   are co-parenting a shared kids/chores sheet. Households top out at the two
   co-parents in practice; a user belongs to **at most one**.

If any of these is genuinely unclear, ask with `AskUserQuestion` — don't guess a
base URL or invent a handle.

## Step 1 — provision the user (new person)

Run on the box (or over Tailscale as an operator). Handles are short and unique
(`sam`, `dana`):

```sh
prefrontal user add <handle> --display-name "<Name shown in nudges>"
# add --operator only if they should manage other users
```

This prints a **token shown once** — capture it now; it can't be re-read (only
rotated). If the handle is taken the command exits non-zero — pick another or
`prefrontal user list` to check.

> No-CLI alternative for a remote operator: `POST /admin/users`
> `{handle, display_name, is_operator}` returns the same one-time token.

## Step 2 — household (only if co-parenting a shared sheet)

Two ways in. Prefer the **self-serve invite** — a co-parent already in the
household mints a code and the newcomer redeems it, no operator step:

```sh
# An existing household member creates a code (expires in 7 days):
prefrontal household invite --user <existing-member-handle>
#   → Invite code: PLUM-7F2Q  (share it, or the <base-url>/kids?invite=PLUM-7F2Q link)

# ...or text the join link straight to the newcomer via Twilio (needs TWILIO_*
# configured — the same account the voice-call escalation uses):
prefrontal household invite --user <existing-member-handle> --sms "+14155551234"

# The new user redeems it (they must be a provisioned user in no household):
prefrontal household redeem PLUM-7F2Q --user <new-handle>
```

(Over HTTP, `POST /household/invites` accepts an optional `{"sms_to": "+1…"}`
to text the link. Twilio unconfigured → the code is still minted and the `sms`
result just reports a no-op, so a failed text never blocks the invite.)

Or the **operator wires them in directly** (also used to create the very first
household):

```sh
prefrontal household add "The Kims"        # prints a household id, e.g. 3
prefrontal household join <handle> --household 3
```

Redeem fails loudly and specifically — *invalid / already used / expired /
already in a household*. If the new user is already in another household, an
operator must remove them first: `prefrontal household leave <handle>`.

The co-parent-only features (weekly check-in, delta digest, load-balance view)
light up on their own once the household has ≥ 2 active members — nothing extra
to enable.

## Step 3 — delivery routing (ntfy)

Notifications reach a phone via **ntfy** (Pushover optional). Each person
subscribes their phone to a topic; the deployment publishes to it.

- **Solo / single-user deploy:** the topic is the `NTFY_TOPIC` env var (with
  `NTFY_SERVER`, default `https://ntfy.sh`) — see `docs/deployment.md`.
- **Multi-user deploy:** give this person their **own** topic so their nudges
  don't land on someone else's phone. Their per-user route lives in
  `coaching_state` keys `ntfy_topic` / `ntfy_server` / `ntfy_token`
  (Pushover: `pushover_user_key` / `pushover_token`), which override the env
  defaults for that user. Pick a unique topic like `prefrontal-<handle>-<random>`
  (topics are unguessable-by-obscurity on public ntfy.sh — keep it non-obvious).

Then **verify delivery end-to-end before touching the phone shortcuts** — this
is the fastest way to catch a bad topic/token:

```sh
prefrontal notify --user <handle>            # test push through the real client
prefrontal notify --user <handle> --channel sound -m "hello from Prefrontal"
```

It prints where it routed (ntfy vs Pushover) and the transport result, and exits
non-zero if nothing is configured. Have the person confirm the push arrived.

## Step 4 — connect their email + calendars (optional)

Per-user ingestion is opt-in: a user gets triaged mail and calendar-conflict
detection only for the accounts **they** connect. Each secret (IMAP password /
ICS feed URL) is **sealed at rest**, so this needs a one-time key.

**One-time per deployment** — mint the at-rest key if `prefrontal secrets status`
says it's unset:

```sh
prefrontal secrets init          # prints PREFRONTAL_SECRET_KEY=… → add it to .env
```

Back it up. (Mail is recoverable without it — re-import from env — but a lost key
means re-adding calendar feeds.)

**Mail (IMAP).** Gmail needs an app password (with 2FA on):

```sh
prefrontal mail --user <handle> add-source --account personal \
    --host imap.gmail.com --username <email>    # prompts for the password (not echoed)
prefrontal mail --user <handle> fetch --account personal --heuristic   # verify
```

`--account` is the logical name; repeat per mailbox (work, …). Gmail defaults to
Important-only (`--no-important-only` to fetch all). `list-sources` shows the
accounts, never the passwords. Migrating an existing single-user box off the
global `MAIL_IMAP_*` env? `prefrontal mail --user <handle> import-env-sources`
seals them all in one shot.

**Calendar (private ICS feed).** Paste each calendar's read-only "secret address
in iCal format" (Google: *Settings → Secret address in iCal format*):

```sh
prefrontal calendar --user <handle> add-source --account personal \
    --url '<secret-ics-url>' --me <email>       # --me drops events you've declined
prefrontal calendar --user <handle> sync                              # verify
```

Both keep syncing on a schedule via the `com.prefrontal-mail` /
`com.prefrontal-calendar` launchd jobs (`--all-users`). Full reference:
`docs/design/per-user-sources.md`.

## Step 5 — produce the personalized setup sheet

Now write the newcomer a short, copy-pasteable handoff with **their** values
already substituted, then point to the full docs for depth. Fill the blanks:

- `<base-url>`  → the deployment origin from Step 0
- `<token>`    → their token from Step 1 (or the shared secret on a solo deploy)
- `<ntfy-server>` / `<ntfy-topic>` → from Step 3

Deliver it as this template (replace the placeholders — don't leave them
literal):

---

**Prefrontal setup for `<handle>`**

**1. ntfy (notifications)**
1. Install the **ntfy** app (iOS/Android).
2. Subscribe to topic **`<ntfy-topic>`** on server **`<ntfy-server>`**.
3. You should already have gotten a test push — if not, tell the operator.

**2. iOS Shortcuts (one-tap logging)**
Every shortcut is a **Get Contents of URL** → `POST <base-url>/…` with headers
`X-Prefrontal-Token: <token>` and `Content-Type: application/json`. Start with
these three, then add the rest from `deploy/ios-shortcut.md`:
- **Add Todo** → `POST <base-url>/todos`, body `{ "title": "Provided Input" }`
- **Panic** → `GET <base-url>/panic`, show the `headline` field
- **Made it / Missed it** → `POST <base-url>/webhooks/shortcut`, body
  `{ "action": "made_it", "episode_type": "departure", "channel": "notification" }`

Full catalog (outings, focus, capture, location, departure): `deploy/ios-shortcut.md`.

**3. Scriptable widget (home/lock screen glance)**
1. Install **Scriptable**, tap **+**, paste in `deploy/scriptable/prefrontal-widget.js`.
2. Set `TOKEN = "<token>"` and `BASE_URL = "<base-url>"` at the top.
3. Run once to preview, then add a **Scriptable** widget to the Home Screen
   (Medium) and/or a Lock Screen slot. Details: `deploy/scriptable/README.md`.

**Connectivity note:** every tap crosses Tailscale from the phone — make sure the
phone is on the tailnet. Quick check: open `<base-url>/health` in Safari.

---

## Wrap up

Confirm what actually happened: which user was created (never print the token
back into any file or commit — it's shown once, in the operator's terminal only),
whether they joined a household, that the test push landed, and hand over the
setup sheet. Flag anything skipped (e.g. "no household — solo user").

**Never** write a token or invite code into a committed file, a PR, or a log.
Tokens live only in the operator's terminal output and the new person's phone.
