- **M4: communication-translation tool** ✅ — the ready-now, zero-risk half of
  roadmap M4 ("It does the thing, not just reminds", `docs/roadmap-vision.md` §4):
  a text-only assistant for the *communication* slice of the dreaded admin ADHD
  adults avoid. New `prefrontal/communication_translation.py` (`translate`) does one
  of three jobs on a single message — **decode** (explain what a received, hedged
  work email actually means: the ask, the subtext, the real urgency, any implied
  deadline), **draft** (write a reply from a short description), or **soften**
  (rewrite the user's own message in a chosen **register** — professional / warm /
  firm / concise / friendly). It **only ever returns text** — nothing is sent,
  booked, or written — so it establishes the M4 surface without the trust risk of
  autonomous action (the "promote delegation from drafts to does" execution loop is
  the separate, gated half). Modeled on the `clarify` LLM-with-heuristic-fallback
  idiom: the provider-selected `assistant` client (Claude when opted into Anthropic,
  else local Ollama) does the work; the system prompt forbids inventing facts and
  leaves any missing real detail as a `[bracketed placeholder]`, mirroring the
  delegation-drafting convention. When no model is reachable, **decode/draft decline
  honestly** (`offline: true`, no fabricated result) and **soften degrades** to the
  user's own text with a register note. Surfaced at `POST /communicate/translate`
  (new `communicate` router + `TranslateMessage` schema) and `prefrontal communicate
  --mode … --register …`. Covered by `tests/test_communication_translation.py`
  (mode/register normalization, the model-success path with a fake generator,
  defensive coercion of a malformed reply, transport-error and no-client fallbacks
  per mode, empty input, and the endpoint incl. auth).
