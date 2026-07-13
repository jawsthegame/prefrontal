- **Retire iOS Shortcuts from onboarding & docs** ✅ (epic: Shortcuts → native,
  see `docs/shortcuts-to-native.md`) — the "last mile" of the native migration,
  now that the App Intents and the CoreLocation pipeline have shipped. The
  `onboard-user` skill's setup sheet now leads with the native app (connect QR +
  App Intents on Siri / Action Button / widgets) and relegates the hand-built
  Shortcuts to a clearly-labeled "no paid Apple Developer account?" fallback
  appendix. `README.md`, `docs/deployment.md`, `docs/guide.md`, and `ios/README.md`
  are reframed the same way — native app primary, Shortcuts as the free-signing
  fallback — including the data-flow diagrams (`App Intent / geofence → POST …`
  with the Shortcut as the fallback lane). Docs-only; the endpoints are unchanged.
  *(Remaining sliver, deferred: the server self-description / a richer `source`
  enum — `app_intent`/`geofence` provenance.)*
