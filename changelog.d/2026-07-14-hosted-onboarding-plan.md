- **Hosted / one-tap onboarding — design proposal** 📝 — a plan for the roadmap's
  highest-severity gap (`roadmap-vision.md` §8.1: setup burden is the #1
  abandonment cause) that stays true to all four whitepaper privacy promises. Two
  new rungs beside DIY self-host: **Tier 1** — a one-command installer / appliance
  image that stands up a box the *user owns* and emits the existing
  `prefrontal://connect` claim QR (data never leaves their hardware); and **Tier
  2** — a paid managed *single-tenant isolated* instance with in-instance
  inference, a customer-held at-rest key, and one-click export/eject (no lock-in).
  Binds both to six privacy invariants (data residency, inference locality, sealed
  secrets, relay-not-inspection, minimal control-plane PII, egress transparency)
  and an open-core boundary that keeps the app MIT and self-hostable for free.
  Design only, no code yet: `docs/design/hosted-onboarding.md`.
