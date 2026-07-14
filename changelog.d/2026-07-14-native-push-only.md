- **Delivery is native-push-only now that Prefrontal is iOS-only** ✅ — native
  **APNs** is the sole product push transport (real `UNNotificationCategory`
  action buttons), with the **Twilio** voice call for the `voice`/critical
  escalation and local TTS unchanged. **Pushover was removed entirely**
  (`PushoverClient`, `PUSHOVER_PRIORITY`, the `pushover_*` `Route`/`Settings`
  fields, the `--pushover-*` route flags, and the `PUSHOVER_*` env). **ntfy is
  demoted to a dev-only shim** — off unless `PREFRONTAL_NTFY_DEV=1` — so a
  free-signing iOS build (no `aps-environment` entitlement → no APNs) can still
  receive server-driven push in development; on a product build ntfy never fires
  and the connect QR carries no ntfy hints. The iOS onboarding's notifications
  step now leads with "allow notifications" (native), with the ntfy install/
  subscribe card shown only on a dev build that carried a topic. `prefrontal user
  route` centers on `--apns-token` (the `--ntfy-*` flags remain, labeled
  `[dev shim]`); `prefrontal notify` reports the APNs route. Touches
  `integrations/delivery.py`, `config.py`, `cli.py`, `webhooks/helpers.py`,
  `.env.example`, the iOS onboarding/push files, and the docs
  (README/deployment/guide/multi-tenant/onboard-user/shortcuts-to-native).
  Covered by `tests/test_delivery.py`, `tests/test_apns.py`, `tests/test_cli.py`,
  `tests/test_chores.py`, `tests/test_household.py`.
