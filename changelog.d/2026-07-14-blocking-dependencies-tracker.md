- **Blockers: who's waiting on you** ✅ — a lightweight tracker for the mirror
  image of a todo: someone *else* is blocked until you do a thing (the ball's in
  your court). Capture is one line — `prefrontal blocked add "Sam" "the budget
  numbers"`, `POST /blockers`, or one utterance to the NL assistant ("Sam's
  blocked on me for the Q3 numbers" → `add_blocker`) — and it feeds
  **prioritization**, the whole point: panic mode buckets open blockers into
  already-behind / bearing-down / piling-up (scored a notch above the equivalent
  todo, so a person waiting outranks your own back-burner, with a blocker-specific
  "send them a two-line update" first step), and the morning briefing leads its
  "On your radar" zone with **🙋 Waiting on you**, longest wait first — a
  counterweight to shiny-object syndrome. Resolved (not deleted) once you deliver,
  so the history stays; `GET/PATCH/POST /blockers…`, `prefrontal blocked
  list/resolve/reopen`. New `blockers` table + `BlockersRepo`, pure helpers in
  `prefrontal/blockers.py`. Covered by `tests/test_blockers.py`.
