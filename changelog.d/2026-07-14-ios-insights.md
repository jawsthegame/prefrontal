- **iOS: behavioral Insights screen** ✅ — the native app now makes the learning
  loop's "it gets better the longer you use it" story visible. A new **Insights**
  screen (reached from the **Me** tab) rolls up the side-effect-free `GET
  /stats/data` and `GET /balance` into glanceable cards: **time-estimation bias**
  ("you run ~1.4× over your estimates", plus a per-context breakdown),
  **follow-through** (success rate, current streak, the success/partial/miss
  split, and a recent-outcomes sparkline), **focus balance** (out-of-home time per
  life-domain with per-sphere target meters and the underserved flag), **which
  channel you answer** (per-channel ack rate), **self-care adherence** (typical-day
  average vs. target and nudge→tap latency), and a **feature-usage** summary
  (in-use / ignored / dormant / muted). Pure read with pull-to-refresh; an empty
  history shows a friendly "not enough history yet" state, and focus balance loads
  best-effort so it never blanks the rest. New `InsightsView.swift`,
  `Stats`/`FocusBalance` models, and `APIClient.stats()` / `.focusBalance()`.
