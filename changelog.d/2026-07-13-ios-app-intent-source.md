- **iOS App Intents send `source: "app_intent"`** ✅ (pairs with the
  `POST /webhooks/shortcut` `source` provenance enum) — `APIClient.logShortcut`
  now sends a `source`, defaulting to `app_intent`, so the native Made It /
  Missed It App Intents populate the server's usage provenance instead of
  defaulting to the fallback `shortcut` tag. A hand-built iOS Shortcut still
  omits it and the server defaults to `shortcut`, so /stats can finally tell
  native taps from fallback-Shortcut ones. Client-only (build on a Mac).
