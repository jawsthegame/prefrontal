- **Learned per-user receptivity model (M3)** ✅ — the "right nudge at the right
  moment" graduation, shipped *dormant-until-earned*. `prefrontal/receptivity.py` is
  a transparent, pure, no-deps contextual acknowledgement-rate estimator
  (empirical-Bayes shrinkage toward the user's pooled rate) over cheap features
  already at tick time — coarse local-hour bucket, weekday/weekend, channel class,
  and recent dosage. It predicts, per user and per context, how likely the next
  coach nudge is to be acknowledged, and holds a non-critical cue when that
  probability falls below `coach_receptivity_min_prob` (default 0.34) — *before* the
  user has to ignore three in a row. Trains on the **same** `coach nudge:`
  channel-outcome episodes the learning loop already logs (no new schema). Wired into
  `coaching.decide` behind `receptivity_gate`, sharing the rules gate's contract:
  only ever *removes* non-critical cues, `critical` always passes, fails open,
  forgiving. **The honesty gate:** it is off by default and supersedes the
  rules-based `coaching.receptive` **only** once the learn pass's walk-forward
  `receptivity_calibration` (the twin of `bias_calibration` / `channel_calibration`)
  shows it beats the pooled baseline on that user's held-out history
  (`receptivity_calibration_helps`); on sparse data it stays dormant and the rules
  gate stands — correct, not a bug. There is a `coach_receptivity_learned=off`
  operator kill-switch. The verdict is surfaced honestly in the `learn` CLI output
  and the behavioral profile. Covered by new tests in `tests/test_coaching.py` and
  `tests/test_patterns.py`.
