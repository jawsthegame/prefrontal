- **Turning a module off now means off on every fire path** ✅ — the per-user
  Settings ▸ Features switch (`module_enabled:<key>` = `off`, set from the iOS app
  or the web Settings card) was honored by the native coaching tick but **not** by
  the standalone intervention endpoints, so a module you switched off kept nudging
  from `POST /webhooks/{focus,outing,departure}/check` — the n8n-poll deployment
  path — while going quiet natively. Its twin, the usage-loop mute, *was* honored
  there; the enable overlay had no single-key resolver to call. Adds
  `user_module_off` / `user_module_enabled` to the module registry (the twins of
  `is_muted` / `user_pack_enabled`) and gates all three check routes on it
  (`skipped: "module_off"`, alongside the existing `module_disabled` /
  `module_muted`), including hyperfocus **protection** — a module you turned off no
  longer shields other modules' nudges. Zero-tap `arm_focus_session` (the every-60s
  `prefrontal focus arm` launchd tick + `POST /webhooks/focus/arm`) is gated too: it
  had no enablement check at *either* level, so calendar focus blocks kept
  auto-arming sessions for a module that was supposed to be off. Also brought to
  parity with what deployment-off already does: the `/guide` walkthrough hides a
  module you turned off, `/departure/next` (the widget/Today leave-by) clears with
  Time Blindness, the morning briefing drops its leave-by section, `/balance`'s
  empty-view hint reports *your* trip-tracking state, and the weekly usage nudge no
  longer offers to mute a module you already switched off. The tick's effective
  module set is now one shared `coaching.effective_modules` (deployment − muted −
  user-off), so `coach --dry-run` stops previewing cues that can never fire.
  Covered by `tests/test_disabled_enforcement.py`.
