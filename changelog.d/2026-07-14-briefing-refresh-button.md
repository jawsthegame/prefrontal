- **Briefing refresh button (web + iOS)** ✅ — the morning briefing now has a
  small **↻ Refresh** control that rebuilds it in place from the latest data,
  without a full-page/tab reload. `GET /briefing` is fast and model-free, so a
  re-fetch reflects todos/commitments/self-care/coaching signals that shifted
  since the digest was first shown. Web: a right-aligned button on the "Today's
  briefing" card (`dashboard.html`) that re-fetches and re-renders just that card.
  iOS: an `arrow.clockwise` button in the Morning briefing card header
  (`TodayView.swift`) that re-fetches via the shared `AsyncButton` (spinner +
  error surfacing). Client-only — no server change.
