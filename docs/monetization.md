# Open-source & monetization plan

> **Status.** Draft / strategy document. This is a plan of record for taking
> Prefrontal from "a system its author runs on a Mac mini" to "a project
> technical people run locally *and* a small commercial product for people who
> want it without the wiring." Nothing here is built yet; it sets direction and
> sequencing. Build status stays in [`README.md`](../README.md) and
> [`ROADMAP.md`](../ROADMAP.md).

---

## 1. The core bet

Prefrontal handles the most sensitive data a person has — where they are, what's
on their calendar, what's in their inbox, when they went quiet, how their ADHD
actually plays out day to day. The reason anyone would trust it with that is the
promise in the README: **your behavioral data doesn't leave your network unless
you decide it should.**

That promise is not a feature we can trade away for revenue. It *is* the product.
So the monetization strategy has one hard rule:

> **We never sell, broker, mine, or train on user data, and no paid tier is ever
> "the same product but we get to see your data."** We sell *convenience*,
> *isolation*, *packaging*, and *support* — never access.

Everything below follows from that. The engine stays open source and stays
local-first. What people pay for is that we do the annoying parts (install,
always-on hosting, dependency wrangling, updates, notification relay) *for* them,
in a way that preserves — or in the isolated-VPC case, contractually guarantees —
the same privacy boundary they'd get self-hosting.

---

## 2. Why this needs a plan: the friction inventory

Prefrontal today is genuinely powerful but it is a *builder's* system. Standing
it up (per [`deployment.md`](deployment.md)) means, in rough order:

1. A Mac mini (or other always-on host) you own and keep awake.
2. Homebrew, Python 3.12, a venv, `pip install -e .`.
3. `node@22` (keg-only, force-linked) + npm-installed **n8n** (native
   `isolated-vm` build needs a `distutils`-capable Python in a side venv).
4. **Ollama** + a pulled model sized to RAM.
5. A launchd agent (edit paths), plus two more for the nightly `learn` and mail
   fetch.
6. **Tailscale** for remote access.
7. **ntfy** (topic + phone subscription) and/or Pushover; optionally **Twilio**
   for the 150% escalation call.
8. Importing and wiring **~16 n8n workflow JSONs**, an env-var block, and a
   Header-Auth credential.
9. iOS Shortcuts built by hand for one-tap capture and location.

Every step here is a cliff a non-builder falls off. That friction is *exactly*
the thing worth money — but only if we remove it without breaking the privacy
boundary. The two products in this plan (§4) are two different ways to remove it.

**Strategic corollary — own the orchestration layer.** Steps 3 and 8 (n8n) are
the single biggest source of friction *and* a licensing landmine for anything we
host or bundle (see §7). The codebase has already started down the right road:
the native delivery client (`prefrontal/integrations/delivery.py`) and the
coaching tick (`POST /webhooks/coach/check`, `prefrontal coach --deliver`) mean a
launchd timer can now schedule and deliver nudges **with no n8n at all**.
Finishing that migration — a native scheduler that subsumes what the 16 workflows
do — is the technical spine of this whole plan. n8n stays as an *optional*
power-user integration in the open-source edition; it is not in the path for the
packaged or hosted editions.

---

## 3. What stays free and open (and why that's non-negotiable)

The entire agent engine stays **MIT and local-first, forever**:

- the memory layer, learning pass, and profile summarizer;
- all challenge-area modules and the coaching/triage/panic/encouragement agents;
- the FastAPI webhook surface, CLI, dashboard, and Scriptable widget;
- native local delivery (ntfy/Pushover/TTS) and local Ollama inference.

This is not charity; it's the moat. The audience most likely to pay later — and
to evangelize — is the technical, privacy-conscious ADHD community, and that
audience is repelled by open-core bait-and-switch (crippled community edition,
"real" features paywalled). Keeping the whole *product* open earns the trust that
makes the *services* sellable. This is the ntfy / Tailscale / Plausible playbook:
the code is free, the hosting and packaging are the business. (We already build
on ntfy, which runs exactly this model — worth studying it directly.)

A useful litmus test for any future feature: *does gating this make a
self-hoster's Prefrontal worse, or does it only remove work they'd rather not
do?* We only ever monetize the second kind.

---

## 4. The product ladder

Four tiers, two of them the paid products the brief calls for.

| Tier | Who | What they get | Runs where | Price shape |
|---|---|---|---|---|
| **Community** | Builders, tinkerers, the privacy-maximalist | The full OSS system, self-hosted, hand-wired | Their hardware | Free (MIT) |
| **Prefrontal for Mac** | Has a Mac, wants it effortless, wants data to stay home | Signed one-click app that installs & supervises every dependency | *Their* Mac | One-time or low subscription |
| **Prefrontal Cloud (isolated VPC)** | Wants it, has no always-on hardware, will trade some purity for zero-ops | A dedicated, single-tenant, encrypted VPC we run for them | Our infra, one VPC per customer | Subscription |
| **(Later) Household / Clinician** | Co-parents; coaches & their clients | Multi-seat provisioning, family sheet, coach dashboards | Either paid tier | Per-seat add-on |

### 4a. Prefrontal for Mac — "local, but effortless"

**This is the strongest privacy story and the cheapest to operate**, because
inference and data both stay on the customer's Apple Silicon Mac (Ollama is fast
on M-series; the customer pays for their own compute). We monetize *packaging and
supervision*, not compute — a clean, honest value exchange.

What the app is: a **signed, notarized `.app`/DMG** (not Mac App Store — sandbox
rules make managing launchd, Ollama, and background services infeasible) that:

- bundles or fetches-and-pins Python, the Prefrontal engine, and Ollama;
- runs the engine and the **native scheduler** (§2) as supervised background
  services — replacing the hand-edited launchd plists and *all* n8n wiring;
- ships a menu-bar UI for status, start/stop, "run learn now," and log access;
- handles updates via a proper updater (Sparkle) instead of `git pull`;
- walks the user through Tailscale and notification setup with a GUI, and can
  auto-provision iOS Shortcuts via a shared shortcut link + a QR-coded token;
- pulls a right-sized local model automatically based on detected RAM.

Net effect: the §2 friction inventory collapses to "download, open, answer a few
questions." Data never leaves the Mac. The only outbound calls remain the ones
the user explicitly opts into (calendar feeds, optional Claude reasoning, Twilio).

**Monetization:** one-time purchase (≈ **$59–99**) with a year of updates, or a
low subscription (≈ **$5–7/mo**) that also includes the optional convenience
relays in §5. Sold direct via our own site (Paddle / Lemon Squeezy / Stripe —
they handle sales tax/VAT), not the App Store. The engine underneath is still the
MIT code; the app *wrapper*, updater, and provisioning UX are the proprietary,
paid part (§7).

### 4b. Prefrontal Cloud — the isolated VPC

For people who want Prefrontal but have no always-on Mac and won't self-host.
The privacy promise is preserved by **hard isolation**, not by locality:

- **One single-tenant VPC per customer** — a dedicated small instance with its
  own encrypted volume and its own `prefrontal.db`. No shared database, no shared
  app process. (This is stronger than Prefrontal's built-in row-level
  multi-tenancy, which we keep for the *household* case *within* one customer's
  VPC — see [`multi-tenant.md`](multi-tenant.md).)
- **Encryption at rest** on the volume; **encryption in transit** everywhere;
  keys and secrets per-tenant.
- **Provisioning/control plane** (proprietary) that spins up, configures, backs
  up, updates, and tears down each VPC, and does zero-downtime engine upgrades.
- Access via a per-tenant HTTPS origin (our reverse proxy) or a tenant tailnet;
  the same `X-Prefrontal-Token` auth the engine already uses.
- **Data portability & exit** built in from day one: one-click export of the
  SQLite DB and a "download everything, then delete my VPC" button. A privacy
  product that traps your data isn't one.

**The inference tension — surface it honestly.** A VPC big enough to run Ollama
well is expensive (RAM/GPU). Two options, and the customer chooses with full
disclosure:

1. **Local inference in the VPC** (bigger instance, higher price) — nothing
   leaves the tenant boundary. The purist option.
2. **Cloud reasoning via the Anthropic API** (smaller/cheaper instance) — the
   engine already supports this per-agent via `ANTHROPIC_AGENTS`. This means
   *prompt contents go to Anthropic*. That is a real narrowing of the privacy
   boundary and must be **opt-in, explicit, and covered by our DPA** — never a
   silent default. We pass Anthropic's zero-retention/no-training terms through
   to the customer and document exactly what gets sent.

**Monetization:** subscription, tiered by inference choice and seats:

- *Cloud Lite* (cloud reasoning, 1 user): ≈ **$12–18/mo**
- *Cloud Local* (in-VPC Ollama, 1 user): ≈ **$25–40/mo** (reflects real compute)
- *Household* add-on: +**$4–6/mo** per extra seat (uses built-in multi-tenancy)

Twilio call escalation, if used, is billed as metered pass-through (or the
customer brings their own Twilio key).

---

## 5. Optional convenience services (attach to either paid tier)

Small managed services that remove specific pain without touching the data model.
Each is à-la-carte or bundled into the subscription:

- **Managed notification relay** — a hosted ntfy topic (with auth) and/or a
  Pushover app registration, so users don't run their own. Carries only the
  notification text the engine already emits.
- **Off-box one-tap actions** — the signed `/nudge/act` buttons need a public
  HTTPS origin (`OAUTH_BASE_URL` + `SESSION_SECRET`). We host that origin so
  buttons "just work" without the user standing up public TLS.
- **Twilio escalation** — managed number + metered minutes for the 150% call,
  instead of the user opening a Twilio account.
- **Managed geocoding** — a hosted, rate-limited Nominatim so travel-time
  estimates work without the user running or throttling their own.
- **Encrypted backups** — nightly off-site encrypted `prefrontal.db` snapshots
  (client-side encrypted; we hold ciphertext only). Especially valuable for the
  Mac edition, where the DB otherwise lives on one machine.
- **Reasoning credits** — a metered pool for optional Claude-backed agents, so a
  customer who wants better triage/summaries doesn't need their own Anthropic
  key. (Off by default; billed on use; disclosed as leaving the box.)

These are the honest monetization surface: every one is "we run infrastructure
you'd otherwise run," none is "we look at your data."

---

## 6. Licensing strategy

**The engine is already MIT and stays MIT.** MIT can't be rescinded on code
already published, and we don't want to — see §3. The commercial layer lives
*beside* it, not *inside* it:

- **Open (MIT), public repo:** the engine, agents, CLI, dashboard, widget,
  native scheduler, and n8n workflow templates. Everything a self-hoster needs to
  run the complete product for free.
- **Proprietary, private repo(s):** the Mac app wrapper + updater + provisioning
  UX; the Cloud control plane / provisioner; the billing and relay services. None
  of this is *required* to run Prefrontal — it only removes work.
- **Trademark the name and the brand.** The real moat isn't the code (it's MIT —
  anyone can host it); it's the name, the polish, and the hosted service. Register
  "Prefrontal" (or a product brand if that mark is unavailable) and gate the brand
  behind a trademark policy, exactly as ntfy/Plausible do. Forks are welcome; they
  can't ship *as Prefrontal*.
- **Contributions:** add a lightweight **CLA/DCO** so we retain the right to use
  community contributions in the commercial builds without ambiguity. DCO is the
  lower-friction choice and is usually enough.

We deliberately **do not** relicense the engine to AGPL/BSL. For a niche personal
tool, the hosted-competitor threat is low, and a copyleft/BSL move would spook the
exact self-hosting audience whose trust is the asset. Brand + hosting is a
sufficient moat.

---

## 7. Dependency licensing — the one real landmine

Bundling or hosting third-party software for paying customers has license
consequences. The important one:

- **n8n is fair-code (Sustainable Use License), not open source.** It restricts
  hosting n8n *as a service for third parties* and embedding it in a commercial
  product without a separate license from n8n. **We must not ship n8n inside the
  Mac app or run it inside customer VPCs as part of the paid offering.** This is
  the licensing reason (on top of the friction reason in §2) that the **native
  scheduler must replace n8n** on the packaged/hosted path. n8n remains a fine
  *optional* integration for OSS self-hosters who install it themselves.
- **Ollama** (MIT) and **ntfy** (Apache-2.0) — fine to bundle/host; ntfy even
  models the business we're copying.
- **Tailscale** — the client is open (BSD), but coordination is their hosted
  service; for VPCs prefer a plain per-tenant HTTPS origin, or self-host
  coordination (Headscale) if we want a tailnet story. Don't assume we can resell
  Tailscale.
- **Twilio / Anthropic** — pay-per-use APIs; pass through with clear terms. For
  Anthropic, pass through the zero-retention/no-training posture to customers.
- **Models** — if we auto-pull a model in the Mac app or VPC, pin ones whose
  license permits redistribution/commercial use (e.g. Llama license terms,
  Apache/MIT models). Track this per shipped model.

Action: a one-page **dependency license register** kept in-repo, reviewed before
anything is bundled or hosted.

---

## 8. Regulatory & trust guardrails

- **Not a medical device; make no clinical claims.** Prefrontal is an executive-
  function *support* tool, not a treatment, diagnosis, or medical device. Keep all
  copy in that lane to stay clear of FDA/MHRA device regimes. (The whitepaper
  already frames it as support, not therapy — hold that line in marketing.)
- **Privacy law.** Because we touch location, calendar, email, and ADHD-related
  behavior, treat all of it as sensitive personal data. For Cloud: publish a
  privacy policy and a **DPA**, honor GDPR/CCPA access/export/delete (the export
  and delete buttons in §4b are also compliance features), and disclose every
  sub-processor (the VPC host, Anthropic if used, Twilio if used).
- **HIPAA:** almost certainly out of scope (we're not a covered entity or their
  business associate), but *don't* market to clinicians in a way that drags us in
  until we've taken advice. Flag before building the clinician tier.
- **Security baseline for Cloud:** per-tenant isolation (§4b), encryption at
  rest/in transit, secret management, least-privilege control plane, audit
  logging, and a documented incident process. This is table stakes for asking
  people to trust us with this data off their own hardware.

---

## 9. Go-to-market

- **Audience 1 (free, now):** the technical ADHD / self-hosting / "local-first"
  crowd — Hacker News, r/selfhosted, r/ADHD, Lobsters, the ntfy/Tailscale-adjacent
  communities. Ship a genuinely great free experience; let it spread on the story
  ("an assistant that learns from your failures and keeps your data home").
- **Audience 2 (Mac app):** the same people's less-technical friends/partners,
  and ADHD adults who are Mac users but not builders. The pitch: *everything the
  free version does, zero terminal.*
- **Audience 3 (Cloud):** people with no always-on hardware, and households.
- **Funnel:** free OSS is the top of funnel and the credibility engine. A subset
  converts to Mac (they own a Mac, want zero-ops-local) or Cloud (no hardware).
  The convenience relays (§5) are the natural first upsell for either.
- **Community health:** responsive issues, a public roadmap, and "lived executive-
  function experience welcome" (already the CONTRIBUTING tone) keep the OSS engine
  vibrant, which keeps the top of funnel full.

---

## 10. Sequencing

Roughly three phases; each is independently valuable and shippable.

**Phase 0 — make the OSS story frictionless enough to grow (mostly done + polish).**
- Finish the **native scheduler** so the full nudge/briefing/panic/household set
  runs with no n8n (extends the shipped delivery client + coaching tick). This is
  the prerequisite for *both* paid tiers and removes the biggest OSS friction.
- One-command bootstrap for self-hosters (`prefrontal setup` / a script that does
  init-db + user add + service install), so the manual runbook shrinks.
- Dependency license register (§7); confirm/refresh trademark and CLA/DCO (§6).

**Phase 1 — Prefrontal for Mac (first revenue, cheapest to run, best privacy story).**
- Menu-bar app wrapping the engine + native scheduler + bundled Ollama, with a
  proper updater (Sparkle) and notarized signing.
- GUI onboarding: Tailscale, notifications, iOS Shortcuts via shared link + QR
  token, RAM-based model selection.
- Billing (Paddle/Lemon Squeezy), license-key gating on the *wrapper only*.
- Optional convenience relays (§5) as bundled/attach.

**Phase 2 — Prefrontal Cloud (isolated VPC).**
- Control plane: provision → configure → back up → update → export → destroy a
  single-tenant VPC per customer.
- Encryption at rest, per-tenant secrets, reverse proxy / per-tenant origin.
- Inference choice (in-VPC Ollama vs opt-in Claude) with the DPA and disclosures.
- Household seats via the existing multi-tenancy inside a tenant's VPC.
- Privacy policy, DPA, sub-processor list, export/delete compliance.

**Phase 3 — expansion (validate demand first).**
- Household/family packaging as a first-class paid bundle.
- Clinician/coach tier (only after regulatory advice).
- Deeper managed integrations as customers ask.

---

## 11. Rough unit-economics sanity check

- **Mac edition** — near-zero marginal cost (runs on the customer's hardware; our
  cost is the optional relays they use). A one-time $59–99 or $5–7/mo is almost
  all margin after payment fees. This should fund the rest.
- **Cloud Lite (cloud reasoning)** — small VPC (~$5–15/mo infra) + metered
  Anthropic usage; $12–18/mo leaves thin-but-real margin; watch reasoning spend.
- **Cloud Local (in-VPC Ollama)** — the expensive one; a RAM/GPU instance can be
  $20–40+/mo infra, so $25–40/mo is closer to break-even and exists mostly to
  serve purists who won't accept cloud reasoning. Price it to cover cost, not to
  be cheap.
- Backups, relay, and geocoding are cheap to run and good attach-rate margin.

Takeaway: **lead with the Mac app** — best privacy story, lowest cost, highest
margin — and let it subsidize the more operationally demanding Cloud tier.

---

## 12. Decisions needed before building

1. **Price model for Mac:** one-time + paid upgrades, or subscription? (Affects
   whether the convenience relays bundle in.)
2. **Cloud host:** which provider for the VPCs (isolation, price, regions, ease of
   automation)?
3. **Default inference for Cloud:** ship cloud-reasoning-first (cheaper, needs the
   disclosure) or local-first (purer, pricier) as the headline tier?
4. **Brand/trademark:** is "Prefrontal" registrable in the relevant classes, or do
   we need a distinct product/company brand for the commercial side?
5. **Business entity & tax:** which jurisdiction; merchant-of-record (Paddle/Lemon
   Squeezy handles VAT/sales tax) vs. raw Stripe.
6. **How hard to push the n8n → native-scheduler migration** — it's on the
   critical path for both paid tiers; is it Phase 0's top priority? (Recommended:
   yes.)

---

*This document is a starting point, meant to be revised as the OSS community and
early demand teach us which tier people actually want.*
