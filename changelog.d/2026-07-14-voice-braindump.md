- **Voice brain-dump → structured items** ✅ — the first M1 "capture at the speed
  of thought" primitive. One rambling voice/free-text dump is fanned out to *both*
  existing capture paths and merged into a single review surface: the NL editing
  assistant turns actionable bits into a **previewable** action list (todos,
  commitments, shopping, if-then plans, household facts), and the LLM sensor turns
  behavioral asides ("I keep blowing off admin on Mondays") into **pending**
  candidate updates. Neither half writes on capture — actions apply via the
  existing `POST /assistant/apply`, sensor candidates via `POST /proposals/{id}/accept`
  — so a rambling, imperfect dump can never silently mutate the store. New
  `prefrontal/braindump.py` (composes `assistant.plan` + `sensor.extract_candidates`,
  no new capability or safety model), a `POST /braindump` endpoint, and a
  `prefrontal braindump "…"` CLI (`--file PATH` / `--file -` for stdin, `--apply`
  to execute edits immediately). Covered by `tests/test_braindump.py`.
