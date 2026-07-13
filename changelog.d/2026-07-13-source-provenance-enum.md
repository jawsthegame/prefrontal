- **`source` provenance on one-tap writes** ✅ (epic: Shortcuts → native, closes
  the last sliver in `docs/shortcuts-to-native.md`) — `POST /webhooks/shortcut`
  now accepts an optional `source` on `ShortcutPayload` — `app_intent` (native
  Siri / Action Button / widget), `geofence` (a native location trigger), or
  `shortcut` (a hand-built iOS Shortcut, the default so existing Shortcuts keep
  working). It's threaded into the `engaged` feature-usage event (replacing the
  hardcoded `source="shortcut"`), so /stats can tell native taps from
  fallback-Shortcut ones. The endpoint docstring and the server self-description
  (`prefrontal/__init__.py`, the FastAPI `summary`) are reworded to name the
  native app alongside Shortcuts. Backward-compatible (the field defaults);
  covered by `tests/test_webhooks.py`. *(Follow-up: have the iOS App Intents
  actually send `source: "app_intent"` — a client-side build change.)*
