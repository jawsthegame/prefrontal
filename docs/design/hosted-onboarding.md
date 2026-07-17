# Hosted / one-tap onboarding

Status: **proposal**
Author: drafted with Claude, 2026-07-14

## Problem

The strategic roadmap's honest self-audit names this the **#1, highest-severity**
divergence between the app and its own evidence base
([`roadmap-vision.md` §8.1](../roadmap-vision.md)):

> **The deployment model fights the audience.** Setup burden and ongoing
> maintenance are the #1 abandonment cause for ADHD users … Yet the tool asks its
> user to run a Mac mini, Ollama, n8n, Tailscale, IMAP/ICS config, and launchd
> jobs, driven by a 341-line [`.env.example`](../../.env.example). "Adults with
> ADHD who can self-host" is close to a contradiction … the product path runs
> through a **hosted / one-tap onboarding** option, or the thesis ("the system
> carries the load") leaks back out through the deploy step.

This directly violates **commandment 7 — never add maintenance burden**
("value on day one with zero configuration; no setup-as-hyperfocus-project").
The phone-side onboarding is already solved — a new phone goes from "installed"
to "receiving nudges" by scanning one QR ([`ios-onboarding.md`](ios-onboarding.md)).
The unsolved part is everything *upstream* of that QR: standing up the server the
QR points at.

## Goal & non-goals

**Goal.** Collapse "stand up a Prefrontal deployment" from a day of
brew/Ollama/Tailscale/launchd/`.env` into **run one thing → scan one QR → done** —
*without* breaking any of the four privacy promises the whole product's trust
rests on ([`whitepaper.md` §8](../whitepaper.md)).

**The privacy promises this must keep (verbatim from the whitepaper):**

1. Behavioral data lives in a **local SQLite database** on **your** hardware.
2. Inference defaults to a **local model**; any external API call is **optional,
   explicit, and per-agent**.
3. Remote access is over a **private network**, not a public endpoint.
4. **Nothing leaves your network unless you decide it should.**

**Non-goals.**

- **Not** a public, anonymous, shared-database SaaS. The existing multi-tenant
  design ([`multi-tenant.md`](../multi-tenant.md)) puts several *trusted* people
  (a household) in one shared-DB deployment on purpose; the hosted **service**
  must never co-mingle *strangers'* behavioral data in one DB (see invariant I1).
- **Not** a rewrite of the auth, token, or scoping model — those stay as shipped.
- **Not** closing the source. The Prefrontal app stays MIT and self-hostable for
  free forever (Tier 0 below). Only the *optional* hosted **service** (control
  plane + relay + billing) is a separate, paid, possibly-closed layer. The
  open/service boundary is spelled out in "Open-core boundary" below.

**Success test.** A brand-new user with **no server hardware and no CLI
knowledge** goes from "nothing" to "connected on their phone and receiving a real
nudge" in **under ~10 minutes with zero `.env` editing** — *and* can export their
full data and move to a self-hosted box with one command, having exposed to the
operator only what the tier's written privacy contract says (nothing, for the
owned-hardware tier).

---

## The spectrum, and the decision

"Hosted" spans a privacy↔convenience spectrum. We ship **two rungs** and keep the
DIY rung unchanged, leading with the owned-hardware rung because it kills the #1
abandonment cause while remaining *100% faithful* to all four promises.

| Tier | Who runs the box | Behavioral data | Inference | Privacy posture | Price |
|---|---|---|---|---|---|
| **T0 — DIY self-host** *(today)* | You | Your hardware | Local | All four promises, by construction | Free / OSS |
| **T1 — One-tap onto owned hardware** *(flagship)* | You (a box you own) | Your hardware | Local | All four promises, by construction | Free installer; paid relay optional |
| **T2 — Managed isolated instance** | Us, isolated per user | Our infra, single-tenant + sealed | Local (in-instance) | Honest custodial contract + clean exit | Paid |

T1 is the answer to §8.1: it removes the *setup* burden (the actual abandonment
cause) without moving a single byte of behavioral data off the user's hardware.
T2 exists for people who will never own a box — and buys its convenience with an
*explicit, weaker-but-still-strong* contract (isolation + sealing + no-lock-in),
never by pretending it's the same promise.

---

## Privacy invariants (binding on **both** hosted tiers)

These are the structural rules the design is checked against — the equivalent of
the leak-proofing that made [`multi-tenant.md`](../multi-tenant.md) safe, but for
the hosting boundary. A feature that violates one doesn't ship.

- **I1 — Data residency / no co-mingling.** Behavioral SQLite lives either on the
  user's own hardware (T1) or in a **single-tenant, per-user (per-household)
  isolated volume** (T2). No shared behavioral DB across unrelated tenants. The
  shared-DB multi-tenancy stays *inside* a single trusted deployment only.
- **I2 — Inference locality.** Default inference runs **local to the instance**
  (Ollama in the same box/container). No shared cloud model ever sees behavioral
  prompts. The Anthropic-API path stays exactly as today — per-agent, opt-in via
  `ANTHROPIC_AGENTS` — and **defaults off** in hosted.
- **I3 — Secrets sealed, key held as close to the user as possible.** Source
  secrets (IMAP passwords, ICS feed URLs) stay Fernet-sealed
  ([`prefrontal/crypto.py`](../../prefrontal/crypto.py), `prefrontal secrets init`).
  T2's target is a **customer-held key** (passphrase-derived in the app, never
  stored in the control plane), so the operator cannot decrypt sources at rest.
- **I4 — Relay, not inspection.** The managed remote-access path *replaces* the
  user configuring Tailscale, but is an **authenticated tunnel that terminates at
  the user's own box/instance** — never a data lake. The control plane does not
  log or retain behavioral payloads; TLS terminates as close to the instance as
  the relay tech allows (SNI passthrough preferred, so even in transit the plane
  sees ciphertext).
- **I5 — Minimal PII in the control plane.** The account/billing plane holds an
  email, a box/instance identifier, a relay-hostname mapping, and payment state —
  **nothing else**. No behavioral data, no plaintext tokens (they stay hashed as
  today), no source secrets.
- **I6 — Egress transparency + no lock-in.** The app surfaces exactly what leaves
  ("data flows" panel), and because everything is one OSS SQLite file, the user
  can **export and self-host at any time**. Exit is the trust anchor: the fact
  that you *can* walk is what makes T2's custodial contract acceptable.

A one-page, plain-English version of I1–I6 (per tier) is the user-facing privacy
statement — and, in the spirit of the §8 audit, it states T2's custodial trust
plainly rather than burying it.

---

## Tier 1 — one-tap onboarding onto owned hardware *(flagship)*

Goal: `brew … && ollama pull … && tailscale up && (edit 341-line .env) &&
prefrontal init-db && prefrontal user add … && prefrontal serve && launchctl …`
becomes **one command → one QR**.

### The bootstrap installer

A single entry point — a `curl … | sh` bootstrap, a signed `.pkg`, and/or a
prebuilt **appliance image** for a cheap mini-PC / Raspberry Pi / spare Mac — that:

1. Installs the runtime + Ollama and pulls the default local model.
2. Writes a **minimal** `.env` from sane defaults (see "Config collapse"), runs
   `prefrontal init-db`, calls `provision_user()` for the first operator user,
   and installs the launchd/systemd jobs from [`deploy/`](../../deploy/).
3. *(If the paid relay is enabled — Phase 2)* registers the box with the control
   plane to obtain a stable public hostname + TLS via an **outbound** tunnel —
   no inbound port-forward, no manual tailnet. Otherwise it uses the user's own
   Tailscale exactly as today.
4. Ends by **displaying the claim QR** — the existing `prefrontal://connect`
   payload built by `build_connect_link()`.

### The "one tap"

The user scans the claim QR in the app → connected, first nudge lands. **This
half is already built** ([`ios-onboarding.md`](ios-onboarding.md)); T1's job is to
make the box that emits that QR appear from one command. The
[`onboard-user`](../../.claude/skills/onboard-user/SKILL.md) skill's five manual
steps collapse into installer output.

**Notification delivery — native push as the zero-config default.** Native iOS
push (APNs) is the product transport: it delivers when a device token is
registered and the `APNS_*` creds are configured
([`delivery.py`](../../prefrontal/delivery.py) `deliver()`). The only
other path is a **dev-only ntfy shim** (`PREFRONTAL_NTFY_DEV=1`, off by default)
for free-signing builds with no APNs entitlement. A self-hosted DIY box that lacks
Apple push signing creds leans on that shim during development ("install the ntfy
app, subscribe to a topic"). **Hosted flips this:** the operator holds the `APNS_*`
creds centrally, so native push is the default for every user and the "install a
second app + subscribe to a topic" step — itself real onboarding friction —
**disappears** entirely; the ntfy shim is never needed on a hosted product build.
(The device token registers via `POST /route/apns-token` on first app launch — no
user step.)

### Config collapse *(the honest core of the whole effort)*

The 341-line [`.env.example`](../../.env.example) is itself the abandonment
surface. Audit every key: everything but a small required set (`PREFRONTAL_MODULES`
already defaults to "all") gets a working default in
[`config.py`](../../prefrontal/config.py), so a fresh install has **value on day
one with zero configuration** (commandment 7). This is a pure OSS win that helps
Tier 0 self-hosters *today*, independent of any hosted service.

Control plane exposure in T1: account email, box id, relay-hostname mapping,
billing. **Zero behavioral data** — the tunnel terminates at the box, TLS
passthrough. I1–I6 hold fully, by construction.

---

## Tier 2 — managed isolated instance

For users who will never own a box. We run it — *faithfully*:

- **Single-tenant isolation (I1).** One container/VM + one SQLite volume **per
  user (or per household)**. Isolation is the structural substitute for "your
  hardware" — never the shared-DB multi-tenancy.
- **In-instance inference (I2).** Ollama runs inside that same instance; no
  shared model. (Resist the cost temptation of a shared GPU pool — see Open
  questions.) Anthropic API stays opt-in and off by default.
- **Customer-held at-rest key (I3).** `PREFRONTAL_SECRET_KEY` derived from a
  passphrase the user sets in the app, never persisted in the control plane, so
  IMAP/ICS secrets are sealed even from the operator. (Lost passphrase = re-add
  sources — an acceptable, documented tradeoff, matching the existing
  [`secrets`](../../.claude/skills/onboard-user/SKILL.md) note.)
- **Clean exit / no lock-in (I6).** One-click **export** (the SQLite file +
  `profile-<handle>.md`) and **eject-to-self-host** (hand the same DB to a Tier 1
  box). Cancellation triggers documented deletion.
- **Explicit contract.** T2's privacy statement says plainly: *your data is
  isolated and sealed, but you are trusting us as the custodian of the instance;
  if you need "never leaves my control," use Tier 1.* That honesty is the §8
  audit's own ethos.

T2 makes us a data **processor** (GDPR/CCPA); that regulatory posture is a T2-only
cost T1 sidesteps (Open questions).

---

## Account & signup layer *(shared by T1 relay + T2)*

- Reuse the session-cookie machinery in
  [`webhooks/oauth.py`](../../prefrontal/webhooks/oauth.py) — but note today's
  OAuth is **login-only against an allowlist** (`GOOGLE_OAUTH_ALLOWED`). The
  hosted service needs **open signup**, which lives in the **control plane**, a
  service *separate from* the per-instance app (keeping I5: the app never gains a
  signup surface that could hold cross-tenant PII).
- Minimal PII (I5). Billing on the paid tiers funds the free OSS core.

### Open-core boundary

| Stays MIT / self-hostable / free | Optional paid **service** (may be closed) |
|---|---|
| The whole Prefrontal app, CLI, iOS app, modules, memory, schema | Control-plane signup/billing |
| Tier 0 DIY + the Tier 1 **installer** and appliance image | The managed **relay** (hostname + tunnel) |
| Export / eject tooling; `prefrontal://connect`; all primitives | Tier 2 managed-instance provisioning/hosting |

"Open sourced and free" stays literally true for anyone who self-hosts; the
service sells *convenience*, never *capability*.

---

## Reused primitives (little new invention)

| Need | Existing primitive |
|---|---|
| The "one tap" | `prefrontal://connect` + QR, `build_connect_link()` ([`cli.py`](../../prefrontal/cli.py)), `ConnectPayload.swift` |
| Auth | per-user hashed tokens ([`multi-tenant.md` §6](../multi-tenant.md)) — unchanged |
| First-user seeding | `provision_user()` / `prefrontal user add`, called by the installer |
| Secrets at rest | Fernet ([`crypto.py`](../../prefrontal/crypto.py)), `secrets init` → customer-held-key model |
| Co-parent join | household invites — unchanged |
| Web account | `oauth.py` session cookie, extended from allowlist-login to control-plane signup |
| Always-on + updates | [`deploy/`](../../deploy/) launchd/systemd, `update.sh`, `package.sh` |
| Per-instance jobs | the `--all-users` nightly fan-outs (`learn`/`summarize`/`coach`/mail/calendar) |

---

## Phased build

- **Phase 0 — Config collapse + privacy-contract doc.** Shrink mandatory `.env`
  to near-zero (defaults into `config.py`); publish I1–I6 as the per-tier
  privacy statement. Pure OSS win; unblocks everything; helps DIY today.
- **Phase 1 — One-command installer (T1, no relay).** Script / appliance image →
  running box + claim QR, still over the user's own Tailscale. Ships **most of
  the abandonment win** on its own.
- **Phase 2 — Managed relay + hosted hostname + auto-update (T1 full).** Control
  plane issues hostname + outbound tunnel (drops the Tailscale-setup step);
  account service + billing.
- **Phase 3 — Managed isolated instance (T2).** Provision-per-signup, in-instance
  inference, customer-held key, export/eject, deletion-on-cancel, DPA posture.
- **Phase 4 — Provision-from-app.** Close the [`ios-onboarding.md`](ios-onboarding.md)
  "future" gap ("provisioning *from* the app"): sign up and create the user
  inside the app, so T2 has **no operator/CLI step at all**.

Phases 0–1 are shippable to OSS self-hosters with no service dependency and
deliver the bulk of the §8.1 fix. Phases 2–4 stand up the paid service.

---

## Touch list (where the work lands)

| Area | Files | Change |
|---|---|---|
| Config collapse | `config.py`, `.env.example` | Defaults for all-but-required keys; shrink the example |
| Installer | new `deploy/bootstrap.sh`, appliance image build, `package.sh` | One-command stand-up → claim QR |
| Relay client | new `prefrontal/integrations/relay.py` (Phase 2) | Outbound tunnel registration; hostname claim |
| Control plane | **new service repo** (out of this tree) | Signup, billing, hostname mapping, tunnel broker |
| Customer-held key | `crypto.py`, `secrets`, iOS key-entry | Passphrase-derived `PREFRONTAL_SECRET_KEY` (T2) |
| Export / eject | new `prefrontal export` / `prefrontal eject` CLI | SQLite + profile bundle; import into a T1 box |
| Provision-from-app | `oauth.py`, iOS onboarding | In-app signup + user creation (Phase 4) |
| Egress panel | dashboard / iOS settings | "data flows" transparency view (I6) |
| Docs | this doc, `deployment.md`, `README.md`, `onboard-user` skill | Repoint onboarding at the installer/QR |

---

## Open questions

- **Relay technology.** Tailscale Funnel vs. a self-run reverse tunnel vs.
  Cloudflare Tunnel — each has a different I4 profile (who can see SNI / plaintext,
  where TLS terminates). Prefer whichever keeps the plane blind to behavioral
  payloads.
- **T2 inference economics.** A small in-instance model per tenant is the
  privacy-correct choice (I2) but costs more than a shared GPU. The shared-pool
  temptation must be resisted; quantify the per-instance cost before committing
  T2 pricing.
- **Customer-held-key UX vs. lockout.** Passphrase loss = re-add sources. Confirm
  that's the accepted failure mode (it matches the existing secrets note) and
  design the recovery/warning UX.
- **Regulatory posture (T2 only).** Data-processor duties (GDPR/CCPA, DPA,
  deletion SLAs). T1 largely sidesteps this; scope it as a T2 gate.
- **Timezone / multi-box households.** A household on T2 = one instance, one
  timezone assumption — same open question flagged in
  [`multi-tenant.md` §13](../multi-tenant.md).

---

## How we'll know it's working

Tie to the roadmap's retention north-stars ([`roadmap-vision.md` §7](../roadmap-vision.md)):

- **Time-to-first-nudge** from signup — the setup-friction metric this whole
  effort exists to move.
- **Setup-abandonment rate** — how many starts never reach the first nudge
  (T1/T2 vs. DIY).
- **4-week retention delta**, hosted vs. DIY — does removing setup burden actually
  move durable use, as §8.1 predicts.
