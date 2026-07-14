- **Learning §2: sensor proposal-durability check** ✅ — the post-acceptance
  *outcome* half of the LLM-sensor feedback loop, complementing the shipped
  precision (accept-rate) check. The literal "did §4's numeric calibration improve
  after accepting a proposal?" framing was ill-posed — none of the proposable state
  keys (`responsive_hours_*`, `self_care`, `encouragement`,
  `preferred_briefing_format`) or the (number-free) inferred episodes feed the
  numeric bias, so no accepted proposal has a causal path to it, and broadening the
  allowlist to force one would loosen the sensor's conservative write model. The
  honest analog that *is* measurable: `compute_proposal_durability` /
  `recompute_proposal_durability` (`prefrontal/sensor.py`) check whether each
  accepted `llm_inferred` setting is **still standing** or was later changed away by
  an explicit edit — comparing each key's live `coaching_state` value against the
  latest accepted proposal for it. Runs in the nightly `learn` pass, persists
  `sensor_durability_rate` / `sensor_durability_samples` / `sensor_reversed_targets`,
  and surfaces in the profile and `prefrontal proposals stats`. Because
  `coaching_state` keeps no history it's a one-snapshot bit per key, so it's a
  **diagnostic** (not auto-fed into `avoided_state_keys`), the way §4 leaves drift a
  surfaced diagnostic. Covered by `tests/test_sensor.py`.
