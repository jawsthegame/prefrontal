- **iOS: on-device brain-dump now catches if-then plans and behavioral asides**
  ✅ — the on-device Foundation Models parse (roadmap M1) used to scope itself to
  the four "actionable" item kinds (todos, commitments, shopping, blockers) and
  escalate everything subtler to the cloud pass. It now also extracts the two
  cases the server seam already accepted but the client never sent: **if-then
  plans** ("when I get home, I'll take my meds") — emitted as `add_if_then`
  actions once a machine-detectable cue is present (a place, an `HH:MM-HH:MM`
  window, or `arrive_home`/`leave_home`), dropped client-side when none is, so we
  never post a plan that could only come back as a dropped-item error — and
  **behavioral episodes** ("I blew off admin again") — emitted as pending sensor
  `observations` in the same `POST /braindump` body (previously hard-coded `[]`).
  `episode_type`/`outcome` are constrained to the server's allowlists via guided
  generation so items survive validation instead of silently dropping. A *settings
  change* (a `state` candidate like "stop nudging me after 9") is deliberately
  still left to the cloud pass — a hallucinated one would propose editing the
  user's own preferences, a sharper edge than logging an episode. Server-side
  unchanged: `assistant.plan_preparsed` already validates `add_if_then` and
  `sensor.validate_observations` already allowlist-checks episodes, and everything
  still lands as a preview/pending the user confirms — the human-in-the-loop safety
  model is untouched. New `IfThenPlan`/`Observation` `@Generable` types and
  `ParsedBrainDump.wireObservations`; `BrainDumpTests` covers the widened parse
  body. Client-only (build on a Mac).
