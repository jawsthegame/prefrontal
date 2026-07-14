- **Apple Watch companion (v1)** ✅ — a native watchOS app (`ios/PrefrontalWatch`)
  that relays every request through the paired iPhone over WatchConnectivity, so
  it holds no token and makes no network calls of its own (the API is
  Tailscale-only, where a standalone Watch usually can't reach). The phone runs
  the existing `APIClient`/`Endpoints` call and relays the JSON back; the watch
  reuses the phone's `Models`. Surfaces: a **Today** glance (leave-by / do-now /
  free time), one-tap **self-care** logging, **quick actions** (I'm back, wrap up
  focus, Panic, dictated add-todo), and watch-face **complications**
  (inline / circular / rectangular). Capture writes queue via `transferUserInfo`
  when the phone's unreachable (at-least-once, like the phone's `OfflineQueue`);
  lifecycle writes require reachability. New shared `Prefrontal/Watch/` types +
  `PhoneWatchConnectivity` on the phone. Client-only (build on a Mac); the watch
  complication's App Group needs a paid team, like the phone widget.
