- **iOS: self-care end-of-day gap review** ✅ — the **Me** tab now surfaces the
  opt-in evening recap it was missing. A **Day review** card reads the
  side-effect-free `GET /self-care/review` and shows the gaps a raw tally hides
  (a late first glass, a long stretch between breaks, a quota finished short) as
  a list, plus "on track" win chips for the checks that met their bar cleanly —
  or a "no gaps today, nicely spaced" note on a clean day. It's a pure read
  (pull-to-refresh alongside the self-care chips) and stays hidden unless
  self-care is on and something's been logged or is due, so an idle day stays
  quiet. New `SelfCareReview` model + `APIClient.selfCareReview()`.
