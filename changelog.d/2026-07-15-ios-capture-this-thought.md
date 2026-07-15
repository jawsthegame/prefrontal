- **iOS "capture this thought" → the sensor path** ✅ — deepens zero-friction
  native capture on top of the existing App Intents backbone. A passing thought
  now reaches the LLM-as-sensor (`POST /observe`) from three native surfaces, all
  landing in the same path: the **Action Button / Siri** dictate a thought in the
  background with no app launch (`CaptureThoughtIntent`, `openAppWhenRun == false`);
  the **interactive Home Screen widget** grows a ✎ one-tap capture button; and a
  new **Control Center / Lock Screen control** (iOS 18) does the same
  (`CaptureThoughtControl`). Because the widget and Control Center can't collect
  free text inline, they open the app's pre-focused quick-capture sheet
  (`Views/CaptureThoughtView.swift`) via a shared App-Group hand-off
  (`SharedStore.requestCapture()` → `RootView`), also reachable as a
  `prefrontal://capture` deep link. The sensor only *proposes* pending candidate
  updates for review — nothing authoritative is written on capture — so the tap is
  safe from anywhere, and `APIClient.observe` is `queueable` so a thought jotted
  off the tailnet replays on reconnect instead of being lost. "Capture a Thought"
  is registered as a Siri/Action Button phrase (`Log Trip` drops its auto-phrase to
  stay within the 10-entry `AppShortcutsProvider` cap; it's still a full intent).
  Client-only (build on a Mac); covered by `ios/PrefrontalTests/APIClientTests.swift`.
