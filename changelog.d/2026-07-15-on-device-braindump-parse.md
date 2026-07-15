- **Brain-dump: on-device Foundation Model parse, cloud only for hard reasoning**
  ✅ — the next M1 "capture at the speed of thought" step. `POST /braindump` now
  accepts a `parse` the client already produced with its **on-device Foundation
  Model** (Apple Foundation Models / Gemini Nano) instead of raw `text`: the server
  calls **no model at all**, just running the supplied `actions` (wire-format
  editing ops) and `observations` (sensor candidates) through the *identical*
  propose→confirm gates — actions re-validated against the current store and
  previewed (written only on `POST /assistant/apply`), observations allowlist-checked
  and recorded **pending** (`POST /proposals/{id}/accept`). So the cheap/private/offline
  path keeps the exact same human-in-the-loop safety model — an on-device parse is
  untrusted input that still can't write, slip an off-allowlist candidate, or act on
  a stale id. Raw `text` still escalates to the opt-in cloud agent (or local Ollama)
  for the hard reasoning; `provider` reports `on_device` vs `anthropic`/`ollama` so
  the capture funnel can tell the paths apart. New `OnDeviceParse` +
  `plan_braindump(parse=…)` in `braindump.py`, reusing new no-model helpers
  `assistant.plan_preparsed` and `sensor.validate_observations`; `BrainDumpMessage`
  gains an optional `parse` (and `text` is now optional — send one or the other).
  Server-side only; the iOS client that runs the model and posts the parse is still
  ahead. Covered by `tests/test_braindump.py`.
