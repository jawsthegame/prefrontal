- **Vacation mode: location-cued entry suggestion** ✅ — the "automatic by
  location, and it asks you" half of vacation mode. After ~2 nights away from home
  (per-user via `vacation_suggest_after_nights`), the `trip_tracking` module raises
  a one-tap **`🏝️ Ease off`** suggestion; tapping it turns vacation mode on
  (`source=auto`), and it lifts itself when you return. Deliberately a
  **suggestion, not an auto-switch** — a work offsite that trips the dwell gets one
  dismissable ask, never a silent mute — and **fired once per absence** via the
  coaching engine's fire-once guard, so an ignored ask never re-nags (commandments
  9 & 10). The confirm rides the same signed `nudge_actions` one-tap path as the
  other notification buttons (`vacation_confirm`), with a stale-tap-after-home
  no-op. Decision logic is a pure gate in `prefrontal/vacation.py`
  (`should_suggest_vacation` / `suggest_threshold_minutes`). Covered by
  `tests/test_vacation.py`.
