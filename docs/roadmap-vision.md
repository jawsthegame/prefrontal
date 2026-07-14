# Prefrontal — Strategic Roadmap (advance-anchored)

*A multi-milestone product roadmap for Prefrontal, grounded in the clinical
science of ADHD, the lived experience of the people it serves, the graveyard of
tools that failed them, and the technological advances that make new
capabilities possible.*

> **How this differs from [`ROADMAP.md`](../ROADMAP.md).** `ROADMAP.md` is the
> *tactical closeout* doc — the near-term gap between "scaffolded" and
> "finished," almost all of it now shipped. **This** doc is the *strategic* view:
> where Prefrontal goes over the next ~3 years, with each milestone **anchored to
> a major advance** (a real capability unlock, not a wish) and justified by the
> mechanism it serves and the abandonment trap it must dodge. It is a direction,
> not a commitment of dates.
>
> **Status:** written 2026-07. Prefrontal's core is remarkably complete (the
> capture→triage→memory→coaching→delivery pipeline, all seven challenge modules,
> the learning loop, native iOS, Parent/Caregiver packs). This roadmap picks up
> from "core complete" and looks outward.

---

## 1. The evidence base, in brief

Five research streams fed this roadmap (clinical science, the existing-tool
landscape, community/lived-experience, the technology frontier, and the HCI/
academic literature). Full citations in the appendix. The through-lines that
matter:

- **ADHD is a performance disorder, not a knowledge disorder** (Barkley, 1997).
  People know *what* to do and *why*; the deficit is *doing what you know* at the
  **point of performance** — the specific place and time the behavior must
  happen. Interventions that teach or explain fail; interventions that **act at
  the point of performance** (cue, scaffold, reduce friction, externalize) work.
  This is Prefrontal's founding thesis and it is correct.
- **The strongest science is for mechanisms, not products.** Working-memory and
  prospective-memory deficits, delay discounting / temporal myopia, emotional
  dysregulation (Hedges' *g* ≈ 1.17 — arguably ADHD's "fourth core symptom"),
  and delayed circadian phase are all robustly established. **Implementation
  intentions ("if-then" planning)** is the single best-evidenced *technique*
  directly relevant to ADHD (Gollwitzer & Sheeran 2006, *d* ≈ 0.65 general;
  Gawrilow & Gollwitzer 2008 restored Go/No-Go inhibition *to non-ADHD levels* in
  children with ADHD).
- **The tool graveyard is real and its cause is knowable.** Tools fail ADHD
  users not for lack of features but because they **demand consistency, memory,
  and maintenance the ADHD brain can't supply — and then shame the user for it.**
  ~80% of productivity-app downloads are abandoned within two weeks; ADHD users
  overshoot that. Novelty is a depreciating asset: the dopamine of a *new* system
  is exactly what makes it feel like "the one," and its fade guarantees the
  system's death.
- **The rare survivors share a signature:** low-friction, always-visible,
  forgiving, and they externalize the work. In one ADHD adult's purge of 47 apps,
  the three survivors were Apple Notes (instant capture), Calendar (visual time),
  and Voice Memos — all zero-maintenance capture/visibility tools.
- **The unsolved problem across the whole field is engagement/retention, not
  capability.** LLMs already generate good plans; JITAI meta-analysis shows a
  small effect (*g* ≈ 0.15) and **82.6% of trials had adherence/dropout
  problems.** The defensible wedge is not a smarter planner — it is a tool people
  *keep using*.
- **There is a genuine gap to own.** The academic field is child-skewed,
  deficit-framed, and thin on deployed adult tools (46 of 3,538 papers in a 2025
  scoping review). **No validated executive-function JITAI exists for ADHD
  adults**, and **no validated emotion-regulation/mindfulness app exists for ADHD
  adults.** Prefrontal is already standing in that gap.

---

## 2. The design commandments (constraints on *every* milestone)

These are non-negotiable. A feature that violates one of these is a feature that
will be abandoned, no matter how advanced. They are distilled from the clinical
mechanisms and the lived-experience research and they already describe much of
Prefrontal's ethos — this roadmap keeps them load-bearing.

1. **Externalize everything — memory, time, intention.** Never require the user
   to hold, remember, or *feel* internally what the interface can display. If
   it's not visible, it doesn't exist. *(WM/PM deficits; point-of-performance.)*
2. **Activation energy → zero.** Capture is one tap or one utterance; the next
   physical action is always pre-decided and tiny. No forms. *(Initiation
   deficit.)*
3. **Visible beats powerful.** A mediocre widget on the home screen beats a
   brilliant app two folders deep. If the user must *remember to open it*, it has
   already lost. *(Out-of-sight-out-of-mind.)*
4. **Radically forgiving — design for the return, not the streak.** Missed
   days are the norm and *are the data*. No streak-loss, no red overdue-count
   shame engines, no guilt UI. Re-entry is always a clean slate. *(Emotional
   dysregulation; shame → avoidance → abandonment.)*
5. **Cue-based, not time-based; structure plans as if-then.** "If [concrete
   cue], then [tiny action]" beats "remember to do X at 3pm." *(Time-based
   prospective memory is the weakest channel; if-then is the best-evidenced
   technique.)*
6. **Reinforce immediately, variably, never punitively.** Immediate feedback
   fights delay discounting; variety fights novelty decay; punitive gamification
   risks the overjustification effect and the shame spiral. *(Reward/DTD.)*
7. **Never add maintenance burden.** The moment upkeep exceeds payoff, it's
   gone. No system that needs weekly "processing" to stay useful; no
   setup-as-hyperfocus-project; value on day one with zero configuration.
8. **Recruit interest and urgency deliberately** — engagement is a lever, not a
   moral failing — **but never let novelty be the load-bearing motivator.**
9. **Silence is a feature.** Default to *not* interrupting when receptivity is
   low. A badly-timed nudge is an added distraction that erodes trust; a muted
   tool is a dead tool. *(JITAI receptivity; ADHD notification blindness.)*
10. **Don't automate nagging.** For households, the load is a *shared, neutral,
    equally-visible surface* — never a tool one partner uses to assign, monitor,
    or nag the other (that automates the corrosive parent-child dynamic).

---

## 3. Strategic guardrails (the non-goals)

Three traps the research flags explicitly. Staying out of them is as important as
any milestone.

- **Stay a consumer wellness / executive-function *support* tool — not a
  regulated medical device.** Akili's EndeavorRx got the first FDA clearance for
  a prescription game (2020) and still collapsed commercially (~$114K revenue vs.
  $15.3M expenses in one quarter; abandoned the prescription model; fire-sold for
  ~$34M in 2024). Payers wouldn't reimburse; prescribing friction killed
  adoption. FDA's Jan-2025 draft guidance also flags that non-deterministic /
  generative AI is a *hard fit* for existing device pathways. **Disease-treatment
  claims turn Prefrontal into a device with an unsolved LLM-clearance path.** Keep
  claims in "general wellness / EF support" territory and ship freely. *(An
  optional, validated efficacy study — see §7 — is a credibility asset, not a
  regulatory obligation.)*
- **Don't build a core loop on the iOS Screen Time API (yet).** FamilyControls/
  DeviceActivity/ManagedSettings are notoriously unreliable for a self-directed
  *adult* app — a 6 MB extension memory limit that causes crashes, full
  authorization effectively requiring a *child* account, and unaddressed iOS-26
  regressions. In-the-moment app-shielding is high-value but currently a shaky
  foundation *and partly outside our control.* Treat it as gated (M5).
- **Don't promise open-ended agentic autonomy.** Computer-use agents score
  ~32–38% on real-world task benchmarks — they fail roughly two of three tasks.
  Prefrontal should do **scoped, verifiable, API/MCP-based** actions, never
  drive a browser to "book my dentist." *(M4.)*

And the standing architectural commitments from the whitepaper hold throughout:
**local-first by default**, per-agent opt-in cloud, data stays on the user's
network.

---

## 4. The milestones

Each milestone is **anchored to a major advance**, justified by the **mechanism**
it serves, **composed from Prefrontal's existing primitives** where possible,
guarded against a specific **abandonment trap**, and paired with a **success
signal** the learning loop can actually measure.

Rough sequencing from mid-2026; deliberately not hard dates.

---

### M0 — Baseline: "The assistant that carries the load" *(shipped)*

Where Prefrontal is today. The capture→triage→memory→coaching→delivery pipeline;
all seven challenge modules (Time Blindness, Task Paralysis, Hyperfocus,
Impulsivity, Location-Aware Anchor, Self-Care, Closed-Loop Trip Tracking);
the recency-weighted, context-conditioned learning loop with honest walk-forward
calibration; the LLM-as-sensor capture path (proposes, human accepts); native
iOS (App Intents, interactive widgets, Live Activities, CoreLocation, AlarmKit);
native APNs push / Twilio delivery with escalation; encouragement/panic recovery modes; the
Parent and Caregiver Context Packs with the shared household sheet.

This is already a genuinely differentiated tool. The milestones below are about
*widening the front door* (capture), *making the brain always-visible*, *timing
the reach-out*, and *closing the loop from reminding to doing*.

---

### M1 — "Capture at the speed of thought"

**Anchor advance (READY NOW):** production-grade streaming voice ASR + on-device
LLMs (Apple **Foundation Models** framework, iOS 26; Gemini Nano) + frontier
**multimodal vision**. All shipping in 2026.

**Mechanism / why it matters.** The #1 lived-experience cause of abandonment is
**capture friction**: if logging a thought is harder than losing it, the thought
is lost and the learning loop starves. Voice is the lowest-friction human input
("just open your mouth"), and a ~3B on-device model can parse a rambling
brain-dump into structure *privately, instantly, offline* — a perfect fit for
Prefrontal's local-first stance. Multimodal vision attacks the ADHD experience of
visual clutter directly. *(Design commandments 1, 2, 7.)*

**What to build (on existing primitives):**
- **Voice brain-dump → structured items.** Speak an unstructured ramble; the
  existing NL-assistant (`assistant.py` `ALLOWED_OPS`, already preview-before-
  write) plus the sensor path (`sensor.py`, propose→accept) turn it into todos,
  commitments, household facts, and shopping items. Route through the on-device
  Foundation Model for the cheap/private extraction; escalate to the opt-in cloud
  agent only for hard reasoning.
- **"Photograph the chaos."** A photo of a whiteboard, a mail pile, a scrawled
  sticky note, or a messy desk → structured tasks via multimodal vision. Wires
  into the same triage → propose → accept path.
- **Deepen zero-friction native capture.** Action Button "capture this thought,"
  interactive-widget and Control Center one-tap capture, all feeding the sensor
  path. (App Intents backbone already exists.)

**Abandonment trap guarded:** no forms, no setup, no maintenance. Capture must
never feel like data entry. Rambling, imperfect input is the *expected* input.

**Success signal:** capture volume and *capture-to-first-action* latency; share
of captures that survive to a completed or consciously-dropped item (vs. rotting
in an inbox). The learning loop already logs episodes — instrument capture as its
own funnel.

---

### M2 — "The always-visible external brain"

**Anchor advance (READY NOW, maturing):** mature iOS glanceable surfaces —
**Live Activities / Dynamic Island**, interactive widgets, Control Center controls
— plus **owning the longitudinal memory layer** (don't rely on any model's
built-in "memory"; a structured behavioral model retrieved into context).

**Mechanism.** *Out of sight, out of mind* is neurological reality, not a saying,
and **time blindness** is the most consistent ADHD deficit a tool can visibly
counter. The single most durable win across every tool studied is **making time
physical and the next thing always visible** (Structured's timeline, Time
Timer's draining disk, Tiimo's spatial day). "Visible beats powerful." *(Design
commandments 1, 3.)*

**What to build:**
- **Persistent time-externalization.** Extend the existing focus-session Live
  Activity into a general "this has been running 22 min" glance on the Lock Screen
  / Dynamic Island for the *current* task or outing — count-up and count-down,
  visible without opening the app. Directly attacks time agnosia and magnified-in-
  hyperfocus time loss.
- **"One next thing," always honest.** A home/lock-screen widget that shows the
  single next action — reusing Prefrontal's *honest* prioritization (the avoided-
  but-important todo, the mid-flight task pinned) rather than a flat list. Never
  show the whole mountain. *(Overwhelm is an indictment; one thing is an
  invitation.)*
- **Visual day-shape.** A timeline widget (the Structured/Tiimo pattern) rendered
  from commitments + fitted todos, so the day's shape is glanceable and concrete.
  Mind the CHI-2024 finding that ADHD users read color/contrast in charts
  differently — design the time UI accordingly.
- **Longitudinal model, retrieved.** Evolve the profile summarizer from a
  compressed prose snapshot toward a queryable behavioral model ("you've
  rescheduled this four times") retrieved into agent context — continuity is the
  external-brain payoff generic reminder apps can't match.

**Abandonment trap guarded:** the surfaces are read-mostly and forgiving — no
streak, no overdue-red, no guilt. Re-entry after a lapse shows *today*, not a
backlog of failures.

**Success signal:** glance frequency; do the always-visible surfaces reduce
missed departures / abandoned outings vs. push-only? (drift metrics already
exist).

---

### M3 — "The right nudge at the right moment" *(the flagship)*

**Anchor advance (~1–2 YEARS):** genuinely **learned proactive / ambient
timing** — the shift from rule/threshold firing to a model of *when a nudge is
welcome*. Grounded in the mature **JITAI** framework (Nahum-Shani et al.) and fed
by newly-cheap context (calendar gaps, location transitions, phone-idle, wearable
sleep/HRV as *gentle* context via HealthKit/Oura/WHOOP).

**Mechanism.** This is the highest-value future unlock for ADHD and the one to
build the identity of the product around: turning a *passive reminder box* (which
ADHD users forget to open) into an assistant that surfaces the right thing at the
right moment — "you have a 30-min gap before your 2pm; want to knock out the form
you've dodged twice?" But the timing is the whole game: **a mistimed nudge is an
added distraction that gets the tool permanently muted** — an especially fatal
outcome for this population. *(Design commandments 5, 6, 8, 9.)*

**What to build (on the coaching tick):**
- **Adopt the JITAI six-component model as the data model** for the existing
  coaching engine (`coaching.py`): decision points, intervention options,
  tailoring variables, decision rules, and the two Prefrontal didn't originally
  model explicitly — **states of receptivity** and **states of vulnerability** —
  both now modeled (receptivity below; vulnerability next). *(Vulnerability gate
  ✅ shipped: `coaching.vulnerable` holds non-critical cues for a bounded cooldown
  after the user reaches for in-the-moment emotional support — the honest,
  already-logged "state where a nudge could do harm" — with a longer hold after a
  crisis screen. Same contract as the receptivity gate: `critical` always passes,
  pure, fail-open, tunable via `coach_vulnerability_minutes` /
  `coach_vulnerability_crisis_minutes`.)*
- **Learn interruptibility per user.** Extend the already-shipped
  `channel_response` learning into a receptivity model: *when* is this person
  reachable, and when is silence correct? Default to silence when receptivity is
  low. Log every fired/held decision (the tick already does) and graduate from
  heuristics toward a contextual bandit (start heuristic, log everything — the
  HeartSteps/Oralytics playbook). *(Rules-based cut ✅ shipped:
  `coaching.receptive` holds non-critical cues after a run of consecutive ignored
  nudges — "default to silence when not receptive" — reusing the `coach nudge`
  outcome episodes, forgiving on a single ack, `critical` exempt.)* *(Learned model
  ✅ shipped, dormant-until-earned: `prefrontal/receptivity.py` is a transparent,
  pure, no-deps contextual acknowledgement-rate estimator over hour bucket /
  weekday / channel / recent dosage, trained on the same `coach nudge` episodes. It
  gates non-critical cues by predicted P(ack) — but **only after** a walk-forward
  `receptivity_calibration` (the honesty twin of `bias_calibration` /
  `channel_calibration`) shows it beats the pooled baseline on that user's held-out
  history; until then, and on sparse data, the rules gate stands. This is the
  "learned" claim earned rather than asserted.)* *(Dosage cap ✅ shipped:
  `_apply_dosage_cap` in `decide` holds interrupting non-critical overflow past
  `coach_daily_nudge_cap`/day — highest-urgency wins the budget, the rest re-offer
  next tick — so a bad day can't barrage. `critical`/`digest` exempt.)*
- **Fuse if-then plans with the trigger moment.** Surface the user's pre-set
  implementation intention *at* its cue (the best-evidenced technique meeting the
  best-timed delivery). This is a small, cheap, deeply-evidenced feature.
- **Wearable-as-context, honestly.** Sleep/HRV adjust *tone and expectations*
  ("slept 5h — let's pick one thing today, not ten"), never a clinical state
  claim. Respect the delayed circadian phase — don't assume 9pm wind-downs.

**Abandonment trap guarded:** habituation and mistiming. JITAI evidence is blunt
that nudge effects *decay* and shrink when recent dosage is high — so hard
frequency/dosage guardrails and receptivity-gated silence are built in from day
one (Prefrontal's quiet-hours + debounce are the seed).

**Reliability gate:** the *timing* is the gate, not model capability. The
rules-based version shipped first; the learned receptivity model now ships too but
**earns** the "learned" claim rather than asserting it — it only takes over gating
once it demonstrably beats the pooled baseline on a walk-forward check
(`receptivity_calibration`, the same honesty bar `bias_calibration` /
`channel_calibration` already set). Dormant-until-earned by construction.

**Success signal:** ack-rate on fired nudges *without* a rise in mute/snooze;
proximal effect (did the nudged action happen more than the un-nudged base
rate?), measured the way a micro-randomized trial would.

---

### M4 — "It does the thing, not just reminds"

**Anchor advance (~1–2 YEARS):** reliable **scoped agentic execution** via
structured tool-calling / **MCP** — a bounded, verifiable action space, *not*
open-ended computer-use agents.

**Mechanism.** The dreaded multi-step admin — the emails, forms, and calls ADHD
users avoid — is a pure task-initiation wall. Reminding someone to do the thing
they're avoiding doesn't move it; *doing* it (with confirmation) removes the wall
entirely. Communication translation (drafting/decoding/softening work email) is a
documented, ready-to-productize LLM use among ADHD adults. *(Design commandment
2; task-initiation deficit.)*

**What to build (on delegation):**
- **Promote delegation from "drafts" to "does."** `delegation.py` already writes
  a research brief and draft comms. Extend it to *complete* bounded, verifiable
  actions via MCP tool-calls: book the calendar event, send the pre-drafted email,
  fill a known form, place a scoped Twilio call — each **previewed and confirmed**
  before it fires, matching the NL-assistant's existing preview-before-write
  contract.
- **Communication translation as a first-class tool:** decode an ambiguous work
  email, draft a reply in the right register, soften a message. High demand, low
  risk, ready now.

**Abandonment trap guarded:** over-reach and loss of trust. Actions are scoped,
verifiable, and confirmable. Never an open browser agent (~1/3 success). A single
wrong autonomous send costs the trust the whole product runs on.

**Success signal:** count of avoided-admin items actually *completed* through the
assistant; user-reported "I finally did the thing I'd dodged for weeks."

---

### M5 — "The in-the-moment focus guard" *(conditional / gated)*

**Anchor advance (GATED, ~1–2 YEARS IF UNBLOCKED):** Apple fixing the **Screen
Time API** (memory limit, adult authorization, iOS-26 regressions). *Outside our
control.*

**Mechanism.** Response inhibition and hyperfocus-on-the-wrong-thing: gentle
interruption when the user *declared* a focus block and drifted into a doom-scroll
("20 min into Instagram during your focus block"). Reliable real-world focus
*detection* is genuinely hard (lab 70–90% → field 55–65%; treat any focus score
as a **noisy probabilistic nudge, never ground truth**), but the CHI-2021 result
is encouraging: a closed detect→intervene loop helps *even with imperfect
detection* if the intervention is cheap and trivially dismissible.

**What to build (only when unblocked):** an opt-in, self-directed focus guard —
gentle shield/nudge on user-declared distraction, always one-tap dismissible,
per-user calibrated, signal-quality gated. Phone-usage signals (unlocks, app
launches, time-since-communication — AUROC ~0.83 for disengagement, on-device,
no camera) are the privacy-friendly first signal; head-pose/consumer-EEG are
optional later additions (*integrate* consumer EEG, never build hardware).

**Abandonment trap guarded:** this is the one that most risks becoming a shaming
surveillance tool. It stays opt-in, self-directed, dismissible, and framed as
help — never punishment. **Do not build the core product loop on it** while the
platform is unreliable.

**Success signal:** self-reported focus-block completion; guard interventions
accepted vs. dismissed-with-annoyance (the mute signal).

---

### M6 — Horizon: research bets *(watch, don't commit)*

**Anchor advances (3+ YEARS):** these are genuine research frontiers. Watch them;
do not stake milestones on them.

- **Anticipatory support (digital phenotyping).** Predict a rough executive-
  function / dysregulation day *before* it lands and pre-empt it. Powerful, but a
  research bet with a poor commercial track record (Mindstrong shut down 2023) and
  starved of signal by iOS privacy limits. Not close.
- **Ambient whole-room presence.** Hands-free verbal body-doubling and "time to
  leave" callouts from a home device — once LLM-upgraded home assistants become a
  dependable third-party surface (Apple's home hub repeatedly delayed). Body
  doubling is the best-evidenced, most-requested mechanic in the whole scan
  (Eagle et al.: a *continuum* from live 1:1 → ambient → async recorded presence;
  even AI/synthetic doubles beat working alone in early trials) — so this is worth
  watching hard, and a *virtual/recorded* body-double is buildable well before the
  ambient-hardware version.
- **Entire assistant private and offline.** When on-device models reach today's
  frontier-cloud reasoning quality, *all* of Prefrontal — including sensitive
  longitudinal reasoning — runs on the user's own device. The ultimate expression
  of the local-first commitment.
- **Objective progress signals** via a validated instrument partnership (e.g.
  QbTest) — optional, partnership-gated, only if paired with a validated measure.

---

## 5. Milestones anchored to major advances — the map

The direct answer to "set milestones based on major advances." Read top-to-bottom
as roughly ready-now → farther-out.

| Milestone | Anchoring advance | Readiness | ADHD mechanism served | Builds on |
|---|---|---|---|---|
| **M1** Capture at the speed of thought | Voice ASR + on-device LLM (Apple Foundation Models) + multimodal vision | **Now** | Activation energy; capture friction; WM offload | sensor.py, NL-assistant, App Intents |
| **M2** The always-visible external brain | iOS Live Activities / interactive widgets + owned longitudinal memory | **Now → maturing** | Time blindness; out-of-sight-out-of-mind | PrefrontalWidgets, Live Activity, profile/learning loop |
| **M3** The right nudge at the right moment *(flagship)* | Learned proactive/ambient timing (JITAI) + wearable/phone context | **~1–2 yr** | Prospective memory; the passive-box failure | coaching tick, channel_response learning, trips/location |
| **M4** It does the thing, not just reminds | Scoped agentic execution (API/MCP) | **~1–2 yr** | Task-initiation wall (dreaded admin) | delegation.py, NL-assistant preview |
| **M5** In-the-moment focus guard *(gated)* | Apple fixing the Screen Time API | **Gated / if unblocked** | Response inhibition; wrong-target hyperfocus | new; phone-usage signals first |
| **M6** Anticipatory / ambient / fully-offline *(watch)* | Digital phenotyping; ambient home LLM; on-device frontier reasoning | **3+ yr** | Dysregulation pre-emption; body doubling; privacy | horizon |

---

## 6. Cross-cutting tracks (continuous, not milestone-gated)

These advance in parallel with every milestone and continue the existing
`ROADMAP.md` threads.

- **Deepen the learning loop.** More context-conditioned patterns, the deferred
  bigger swings (a state-history table for chronic-reversal auto-avoidance; a
  broader — safety-reviewed — proposable allowlist). The honesty checks
  (`bias_calibration`, `channel_calibration`, proposal durability) are the model
  for every future adaptation: *visible* when an adaptation doesn't help, never
  silently asserted.
- **Context Pack breadth.** Caregiver situation tools; new packs (Grad student,
  New job); more tailored surfaces. The Pack abstraction is the cheap,
  shareable extension point — lean on it rather than new modules.
- **Invisible-load / household.** Keep the shared surface *neutral and equally
  visible* (commandment 10). Provenance makes mental load legible; the delta
  digest balances it. Never let it become a nagging proxy.
- **Emotion regulation as a first-class concern.** *(First cut ✅ shipped — the
  `emotion_regulation` module.)* ED is a core, large-effect ADHD feature and there
  is *no validated ER app for ADHD adults* — a real gap. The encouragement/recovery
  layer was the seed; the module now delivers ACT (accept + act on values) and DBT
  distress-tolerance micro-skills **on demand** in a hard moment (`POST /emotion/support`),
  with self-compassion framing for rejection-sensitive moments — "RSD" as lived
  experience, not settled science — and a crisis-language screen that routes to
  resources rather than a skill. Next on this track: **proactive** acute-moment
  detection (context flags the moment, not just the user), a wider skill library,
  and outcome-learning on which skills land for whom.

---

## 7. How we'll know it's working (and an optional credibility asset)

The field's lesson is unambiguous: **the core risk is engagement/retention, not
capability.** So the roadmap's north-star metrics are retention-shaped, not
feature-shaped:

- **Durable use, not honeymoon use.** Does usage survive past the ~2-week novelty
  window that kills most tools? Track 4-week and 12-week active-use retention as
  the primary health metric.
- **Return-after-lapse.** The signature of a forgiving tool is that users *come
  back* after falling off. Measure re-engagement rate after a gap — the metric the
  whole "design for the return" commandment exists to move.
- **Miss-informed improvement.** Prefrontal's thesis is that misses are data —
  so the walk-forward calibration checks should show the model getting *more*
  useful over months of a given user's history.
- **Optional efficacy study.** A waitlist-controlled RCT is startup-achievable
  (Inflow ran one at N=154, 8 weeks; the Todaki scripted-chatbot pilot is a
  benchmark to beat) and would put Prefrontal ahead of essentially every
  commercial competitor — *none* of which has published controlled outcomes. This
  is a **credibility asset, not a regulatory obligation**, and it stays firmly on
  the "general wellness / EF support" side of the FDA line (§3).

---

## 8. Where the app and the evidence diverge today

An honest audit of the *current* build against the research above — verified
against the code, not asserted. Ordered by severity. Each divergence names the
research principle it's in tension with and the milestone/track that addresses
it. (Balancing note: several suspected gaps turned out clean — the docs don't
misuse clinical terms like "RSD" or "object permanence," and the kids' star
charts are earn-only/no-punishment, which *matches* the child behavioral-
intervention evidence.)

1. **The deployment model fights the audience.** *(partial — the join-an-existing-
   deployment path is now near-zero-friction; the operator/self-host burden is
   designed-away but not yet built.)* Setup burden and ongoing maintenance are the
   **#1 abandonment cause** for ADHD users ("the app became another thing to
   maintain"). The *user-joining* half has largely closed: native iOS onboarding
   with a scan-and-go QR / deep-link connect-link, a shared-Keychain token, per-user
   source setup, and **native-push-only delivery** (ntfy/Shortcuts retired as the
   primary path) mean a person joining an existing deployment no longer touches
   config. What remains is the *operator* half — the tool still asks whoever runs it
   to stand up a Mac mini, Ollama, n8n, Tailscale, IMAP/ICS config, and launchd jobs
   (a sprawling [`.env.example`](../.env.example) of hundreds of lines). "Adults
   with ADHD who can self-host" is close to a contradiction, so the product path
   runs through a
   **hosted, no-self-host** option — now specced in
   [`docs/design/hosted-onboarding.md`](design/hosted-onboarding.md) but not yet
   stood up. Defensible *today* (a personal, daily-use tool); the hosted backend is
   the open remainder. *(Cuts against commandment 7.)*
2. **~~Emotion regulation is under-built relative to its centrality.~~** ✅
   **First cut shipped** (`emotion_regulation` module + `prefrontal/emotion_regulation.py`).
   Emotional dysregulation is a *core* feature (Hedges' *g* ≈ 1.17, the "fourth core
   symptom") and no validated ER app exists for ADHD adults — a gap to own. It's now
   a first-class module rather than handled only *indirectly* via encouragement/panic/
   recovery: on demand (`POST /emotion/support`, one tap or a few words) it offers one
   brief, evidence-matched micro-skill — ACT acceptance, a DBT distress-tolerance move
   (paced breathing / grounding / cold reset / radical acceptance), or self-compassion
   framing for the rejection-sensitive moments — fitted to the feeling, with a gentle
   acceptance line optionally folded into the rough-day recovery. **General-wellness
   support, not therapy:** crisis language is screened first and answered with
   resources, never a coping skill. Remaining reach (per the §6 track): proactive
   acute-moment detection, and a wider skill library. *(Delivered by the §6 emotion-
   regulation track.)*
3. **~~The stats screen reintroduces a shame vector the rest of the app avoids.~~**
   ✅ **Resolved** (PR #628). [`prefrontal/stats.py`](../prefrontal/stats.py) had
   computed a **"current success streak"** (trailing consecutive successes) — the
   exact metric shape the abandonment research singles out, since streak-loss
   triggers the "what the hell" spiral (loss-forgiving designs saw ~32% higher
   reactivation than loss-based). It was descriptive, not punitive, but it
   *contradicted the app's own forgiveness ethos* (encouragement, recovery, "design
   for the return"). The follow-through view now drops the streak entirely in favor
   of loss-forgiving signals a single miss can't zero out — **`recent_rate`**
   ("lately"), **`trend`** (up/steady/down), **`returned`** (came back after a
   ≥4-day lapse — the comeback, surfaced exactly where a streak would have broken),
   and **`best_rate`** (a personal best that's never lost). The iOS and web cards
   show a badge that only ever celebrates (`💚 Back at it` › `📈 Building momentum`
   › `Best stretch yet`) and nothing otherwise — never a "you lost it" moment.
   *(Now upholds commandment 4.)*
4. **The single best-evidenced technique is absent as a primitive.**
   Implementation intentions / if-then planning is the most strongly-evidenced
   ADHD self-regulation *technique* (Gollwitzer & Sheeran *d* ≈ 0.65; Gawrilow &
   Gollwitzer restored Go/No-Go inhibition to non-ADHD levels), yet **no if-then
   concept exists in the codebase.** The location anchor is cue-shaped (right
   idea), but there's no explicit "if [cue], then [tiny action]" builder. Low-
   cost, deeply-evidenced, and it fuses with the coaching tick. *(The missing
   primitive behind commandment 5; feeds M3.)*
5. **The scheduling core is time-based, but the evidence favors cue-based.**
   *(narrowing — cue-based if-then now spans place, time, and home-crossing events.)*
   The flagship planning path still fits todos into free *time windows*
   ([`prefrontal/scheduling.py`](../prefrontal/scheduling.py); `/todos/now`,
   `/todos/fit`) and pads *departure times*, and **time-based prospective memory is
   the weakest channel** — event/cue-based is stronger. The if-then module now
   offers the stronger channel directly: a plan can cue on a curated place, a
   home-crossing **event** (`arrive_home`/`leave_home`, edge-detected on the tick),
   or a time band — "when I get home, take out the recycling" fires *at* the
   crossing, not on a clock. The residual gap is the *todo-fitting* path itself,
   still purely clock-windowed. *(Relates to #4 and commandment 5.)*
6. **Escalation cadence vs. habituation/receptivity.** *(closed for receptivity —
   rules-based gate, dosage cap, **and** the learned model all shipped; vulnerability
   modelling remains the open half.)*
   "Escalation is not optional" is well-justified for genuinely time-critical events,
   and quiet hours + debounce + channel-response learning mitigate well. JITAI's two
   levers are now both explicit engine gates: **default to silence when low**
   (`coaching.receptive`: hold non-critical cues after a run of ignored nudges,
   forgiving on a single ack) and **cap the dosage** (`_apply_dosage_cap`: hold
   interrupting non-critical overflow past `coach_daily_nudge_cap`/day, highest-urgency
   first, `critical` exempt). The *learned* form has now landed too:
   `prefrontal/receptivity.py` is a transparent per-user contextual ack-rate model
   (hour / weekday / channel / recent dosage) that supersedes the fixed-threshold
   rules gate — but **only** once `receptivity_calibration` (walk-forward) shows it
   beats the pooled baseline on that user's own history, exactly the honesty bar the
   §4 bias/channel checks set; dormant-until-earned on sparse data. The
   *vulnerability* half of JITAI's model has now landed too (`coaching.vulnerable`:
   hold non-critical cues for a bounded cooldown after the user reaches for
   emotional support — the honest "state where a nudge could do harm" — longer after
   a crisis screen, `critical` exempt, fail-open). What remains is richer *proactive*
   context (wearable sleep/HRV via HealthKit — the M3 anchor's ~1–2yr reach; calendar
   gaps already feed the open-window offer). *(M3's closing reach, mostly closed.)*

**Already well-aligned** (so the audit is calibrated): local-first/privacy;
miss-as-data *for the system*; the encouragement/panic/recovery layer; one-tap
App-Intent capture; cue-based location anchors; earn-only kids' star charts. The
founding thesis — act at the point of performance — is exactly right.

---

## Appendix — sources

Grounding for the claims above. Clinical citations are the standard references in
the literature; verify exact pages/effect sizes before quoting in published
material. The community/lived-experience stream is drawn from ADHD-authored essays
and clinical syntheses (Reddit itself was not directly crawlable in this pass —
a verbatim-quote pass on old.reddit.com / the Reddit API is a worthwhile
follow-up if user voices are wanted in product/marketing material).

**ADHD clinical science.** Barkley (1997, *Psychological Bulletin* 121(1):65–94;
*ADHD and the Nature of Self-Control*) — inhibition/EF model, point-of-performance.
Willcutt et al. (2005, *Biological Psychiatry*) — EF-deficit heterogeneity.
Sonuga-Barke — dual/triple-pathway (delay aversion). Martinussen et al. (2005),
Kasper, Alderson & Hudec (2012) — working memory. Talbot & Kerns (2014),
Altgassen et al. — prospective memory. Jackson & MacKillop (2016, *Biol
Psychiatry CNNI*) — delay discounting. Shaw, Stringaris, Nigg & Leibenluft (2014,
*AJP* 171(3):276–293) & Beheshti et al. (2020, *BMC Psychiatry*, *g*≈1.17) —
emotion dysregulation. Volkow et al. (2009, *JAMA* 302(10):1084–91), Plichta &
Scheres (2014), Tripp & Wickens (dopamine transfer deficit) — reward.
Kooij & Bijlenga — delayed circadian phase. Cortese et al. (2009; 2018 *Lancet
Psychiatry* NMA) — sleep; medication efficacy. Hupfeld, Abagis & Shah (2019),
Ashinoff & Abu-Akel (2021) — hyperfocus. Gollwitzer & Sheeran (2006, *d*≈0.65),
Gawrilow & Gollwitzer (2008) — implementation intentions. Safren et al. (2010,
*JAMA*), Solanto et al. (2010, *AJP*) — CBT for adult ADHD. Deci/Ryan —
overjustification.

**HCI / academic.** Nahum-Shani et al. (2018, *Ann Behav Med*) — JITAI framework.
Klasnja et al. (2015; HeartSteps 2019), Liao et al. (2020, *IMWUT*) — MRT /
contextual bandits. von Lützow et al. (2025, *BMJ Mental Health* meta-analysis,
*g*≈0.15, 82.6% adherence problems). Eagle, Baltaxe-Admony & Ringland (ASSETS '23;
TACCESS 2024), Ara et al. (2025) — body-doubling continuum + AI double. Chen, Meng
& Nie (CHI 2026) — task management as relational/affective. Campbell et al.
(INTERACT 2023) — residual EF pain points. Tan et al. (2025 scoping review, 46/3,538).
Pinto et al. (OzCHI '25), Carik et al. (GROUP '25), Zhu et al. (CHI 2026) — LLMs as
EF scaffolding; prompt-friction; interest-based prioritization. Bixler & D'Mello
(2016), Hutt et al. (2019; CHI 2021), Pielot et al. (2015/2017, AUROC 0.83) —
attention detection reality. Antshel et al. (Inflow RCT, *J Atten Disord* 2025);
Jang et al. (Todaki, 2021). Murray et al. (2025, *JMIR Mental Health*) — ADHD ER
design playbook.

**Technology frontier.** Apple Foundation Models (WWDC 2025); FDA AI-enabled
device draft guidance (Jan 2025); Akili/EndeavorRx commercial collapse (STAT,
2023–24); computer-use agent benchmarks (~32–38% OSWorld); Screen Time API
developer reports (memory limit, iOS-26 regressions).

**Tool landscape (mechanics & abandonment).** Tiimo, Structured, Sunsama,
Amazing Marvin, Routinery, Brili, Llama Life, Focus Bear (time-visible /
guided-ritual / configurable-novelty patterns and their failure modes); Goblin
Tools (adjustable-granularity "spiciness slider"); Focusmate/Flow Club (body
doubling); Reclaim/Motion (auto-schedule to remove the "when?" decision); the
"deleted 47 apps" / "12 apps, 3 helped" firsthand accounts and clinical syntheses
(the novelty-decay, maintenance-burden, and shame-spiral abandonment mechanisms).

---

*Companion to [`docs/whitepaper.md`](whitepaper.md) (the thesis) and
[`ROADMAP.md`](../ROADMAP.md) (the tactical closeout). This doc is the strategic,
advance-anchored view.*
