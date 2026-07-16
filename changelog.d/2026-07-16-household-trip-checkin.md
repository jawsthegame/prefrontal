- **Household: trip check-in** ✅ — "keep me posted while you're out" without having
  to remember. When a household opts in (`trip_checkin_enabled`, off by default),
  a parent who's been out on a trip past a short threshold (~60 min) gets **one**
  push with three one-tap buttons — **🏠 Heading home / ⏰ Running late / 👍 All
  good** — and tapping one relays that status to the other co-parent's device. It's
  per-member and once-per-trip (deduped via `trip_checkin_last_trip` in the
  out-parent's `coaching_state`), rides the existing 15-minute household sweep
  (`run_trip_checkin_sweep`, twin endpoint `POST
  /webhooks/household/trip-checkin/check` and CLI `household trip-checkin-check`),
  and self-suppresses when it's off, nobody's out, the trip's under the threshold,
  or it already prompted this trip. New `household_trip_checkin_notice` +
  `trip_status_*` one-tap actions relaying through `deliver_to_member`. Iteration 1
  of a dictate-and-relay channel: the relay path, opt-in flag, and sweep are the
  reusable seam a future free-text update would build on.
