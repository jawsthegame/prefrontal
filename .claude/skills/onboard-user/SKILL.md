---
name: onboard-user
description: >-
  Onboard a person onto a Prefrontal deployment end-to-end — provision their
  user (or mint a household invite code for a co-parent), then hand them a
  personalized setup sheet for the native iOS app (connect QR, App Intents /
  Action Button, home-screen widget) and native push notifications (APNs), with
  iOS Shortcuts as the free-signing fallback — and optionally connects their own
  email (IMAP) and calendar (private ICS feed) sources. Use when asked to "invite
  a user", "add someone", "onboard a co-parent", "set up a new phone/user", or
  "get <name> onto Prefrontal". Runs the `prefrontal user`/`household`/`mail`/`calendar`
  CLI and fills the new person's handle, token, and base URL into the client
  instructions.
---

# Onboard a Prefrontal user

Take a new person from "not on the system" to "phone set up and receiving
nudges". Two things happen: **membership** (a user account, optionally joined to
a household) and **client setup** (the native iOS app, native push, and the
widget — with Shortcuts as the free-signing fallback — filled in with their real
values).

The canonical setup docs are the source of truth — do **not** duplicate their
steps into the repo, cite them:

- `ios/README.md` — the native app: connect QR, App Intents (Siri / Action
  Button / Spotlight), and its Home/Lock Screen widget. **The primary client.**
- `docs/deployment.md` (§6a) — native APNs push setup (the `APNS_*` signing
  creds); (§"Configure once") the token credential + the ntfy dev-shim env vars
- `docs/multi-tenant.md` — the per-user token / household model
- `docs/design/per-user-sources.md` — per-user email (IMAP) + calendar (ICS) sources
- `deploy/ios-shortcut.md` — the iOS Shortcut catalog, kept as the **free-signing
  fallback** (no paid Apple Developer account → no App Intents/widget)

## Step 0 — figure out which flow this is

Ask only what you can't infer. You need:

1. **Base URL** of the deployment — the Mac mini's Tailscale name/IP, e.g.
   `http://mac-mini.tail-scale.ts.net:8000`. Check `.env`
   (`OAUTH_BASE_URL` / any existing docs) before asking.
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
#   → Invite code: PLUM-7F2Q  (share it, or the <base-url>/household?invite=PLUM-7F2Q link)

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

## Step 3 — delivery routing (native APNs push)

Notifications reach a phone via **native APNs push** — the product transport,
which renders real one-tap notification action buttons. There's **no per-user
topic to hand out**: the app registers its device token on first launch (`POST
/route/apns-token`), and delivery resolves to that token automatically.

- **Ensure the deployment's APNs signing creds are set** (once per deployment,
  operator step): the `.p8` auth key and `APNS_KEY_ID` / `APNS_TEAM_ID` /
  `APNS_AUTH_KEY(_PATH)` / `APNS_TOPIC` in `.env`, plus `pip install
  'prefrontal[apns]'`. Full steps: `docs/deployment.md` §6a. If those are
  configured, this person needs no per-user route — connecting the app (Step 5)
  registers their token and their nudges route to their own device.
- **On a multi-user box**, delivery is still per-user by device token: a user with
  no registered token is *computed* but not delivered to, so nudges never land on
  someone else's phone. Getting the app connected (Step 5) is what registers it.

> **Free-signing dev build?** A build with no paid Apple Developer account has no
> `aps-environment` entitlement and can't receive APNs. For development only, the
> deployment can set `PREFRONTAL_NTFY_DEV=1` and fall back to the **ntfy dev
> shim** — give the person their own topic (`prefrontal user route <handle>
> --ntfy-topic prefrontal-<handle>-<random>`; `--ntfy-server` / `--ntfy-token`
> also available; run with no flags to show the route) and have them subscribe the
> ntfy app to it. ntfy is off by default and never used on a product build.

Then **verify delivery end-to-end** — this is the fastest way to catch a delivery
misconfiguration:

```sh
prefrontal notify --user <handle>            # test push through the real client
prefrontal notify --user <handle> --channel sound -m "hello from Prefrontal"
```

It prints where it routed (native APNs vs the ntfy dev shim) and the transport
result, and exits non-zero if nothing is configured. Have the person confirm the
push arrived. For native APNs this runs **after** the app is connected (Step 5),
since connecting is what registers the device token — there's nothing to push to
until then; the ntfy dev shim can be verified before, since it routes to a topic.

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
- `<ntfy-server>` / `<ntfy-topic>` → only for a free-signing dev build (Step 3's
  ntfy dev-shim fallback); omit the ntfy block entirely on a product build

**If they're using the native iOS app**, lead with the QR — it fills in the base
URL + token by scanning, so they never hand-type the token. Generate it (`--rotate`
mints a fresh token to embed since the original is shown only once):

```sh
prefrontal user connect-link <handle> --qr --rotate    # needs `pip install 'prefrontal[qr]'`
```

Put that QR (or the `prefrontal://connect?…` link) at the top of the sheet. It's
as sensitive as the token — don't commit it or paste it anywhere shared. Once the
app is connected, one-tap logging is **native** (App Intents on Siri / the Action
Button / widgets — nothing to paste) and the widget step below adds the glance.
Only a free-signing install (no paid dev account) falls back to the Shortcuts
appendix.

Deliver it as this template (replace the placeholders — don't leave them
literal):

---

**Prefrontal setup for `<handle>`**

**1. Connect the app** — open Prefrontal and scan the QR on this sheet (or scan
it with the Camera app and tap "Open in Prefrontal"). No app yet? Enter
`<base-url>` and your token by hand in the connect step.

**2. Allow notifications (native push)**
1. On first launch, tap **Allow** when Prefrontal asks to send notifications. That
   registers your device for **native push** — no second app, no topic to
   subscribe to. Nudges arrive as normal iOS notifications with one-tap action
   buttons (Ate / Drank / Made it / …).
2. You should shortly get a test push — if not, tell the operator.

> **Free-signing build only (no push):** if the operator says this build can't
> receive native push, install the **ntfy** app, subscribe to topic
> **`<ntfy-topic>`** on server **`<ntfy-server>`**, and pushes arrive there
> instead. Skip this on a normal install.

**3. One-tap logging (native — no URLs to paste)**
The app ships **App Intents** that authenticate off the shared token and drive the
same endpoints. Log todos, panic, "made it / missed it", going out / I'm back, and
start / end focus from **Siri** ("Hey Siri, add a Prefrontal todo…"), the **Action
Button**, **Spotlight**, or the Home/Lock-Screen widgets — all without opening the
app. Assign your most-used action to the Action Button in **Settings ▸ Action
Button ▸ Shortcut ▸ Prefrontal**. Details: `ios/README.md`.

> **No paid Apple Developer account?** A free-signing install has no App Intents
> or widget, so it falls back to **iOS Shortcuts** for one-tap logging. Each is a
> **Get Contents of URL** → `POST <base-url>/…` with headers
> `X-Prefrontal-Token: <token>` and `Content-Type: application/json`. Start with
> **Add Todo** (`POST <base-url>/todos`, body `{ "title": "Provided Input" }`),
> **Panic** (`GET <base-url>/panic`, show the `headline`), and **Made it / Missed
> it** (`POST <base-url>/webhooks/shortcut`, body `{ "action": "made_it",
> "episode_type": "departure", "channel": "notification" }`). Full catalog
> (outings, focus, capture, location, departure): `deploy/ios-shortcut.md`.

**4. Home/Lock Screen widget (glance)**
Once the app is connected (step 1) the widget reads the same token — no separate
setup. Long-press the Home Screen → **+** → **Prefrontal** → add the Medium
widget (your next departure, the one todo to start now, next commitment,
tap-to-log self-care). For a Lock Screen glance, edit the Lock Screen → tap a
slot → **Prefrontal**. Details: `ios/README.md`.

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
