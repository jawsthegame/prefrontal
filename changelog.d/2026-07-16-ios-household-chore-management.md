- **iOS: chore setup from the phone** ✅ (follows the iOS Household UI) — the
  Household tab's chores card gains full management, not just one-tap Done. A ＋
  adds a recurring shared chore and a long-press on any chore **edits** it,
  **pauses/resumes** its reminders (`POST /household/chores/{id}/enabled`), or
  **removes** it (`/{id}/remove`). The add/edit sheet (`POST /household/chores`,
  which upserts on title) covers the everyday fields: whose job it is (either
  parent or a specific member), whether it belongs to a **routine** (inheriting
  that schedule) or stands alone with its own **weekdays** + **due time** (blank =
  an untimed checklist chore), an optional "why it matters" impact line, and a
  reminders-on toggle — with the title held fixed in edit mode so an edit can't
  fork a second chore. Advanced fields (reminder lead time, away-behavior,
  day-of-month, municipal service) keep their server defaults and stay on the web
  dashboard. "Show all" now includes paused chores (a "paused" chip marks them) so
  they can be resumed. Client-only (build on a Mac); endpoints are the existing
  household router.
