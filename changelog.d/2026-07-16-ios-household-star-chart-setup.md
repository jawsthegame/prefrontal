- **iOS: build star charts from the phone** ✅ (rounds out the iOS Household UI) —
  the app could award stars but not create the chart; now the Agreements & star
  charts card has a ＋ to **create** one and a long-press to **edit** or **remove**
  any plan. The chart editor takes a name, who it's for (a kid or the whole
  household), a reward ladder (`7=movie night, 30=new LEGO`), and an optional daily
  **award-reminder** schedule (weekday chips + time). Create posts `POST
  /household/agreements` (kind `reward`) then `/tiers` — which is what turns it into
  a chart — and, when enabled, `/prompt`; edit re-sends tiers/prompt for the
  existing plan (name + child are the key, so they're fixed), and a plain agreement
  can be given tiers to become a chart. Remove uses `/agreements/{id}/remove` (the
  path fixed earlier this session to clear the star ledger first). New
  `Views/HouseholdChartEditor.swift`, `createAgreement`/`setStarTiers`/
  `setStarPrompt`/`removeAgreement` endpoints, and `structured` (tiers + prompt)
  now decoded off the sheet for edit prefill. Client-only (build on a Mac).
