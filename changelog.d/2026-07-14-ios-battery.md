- **iOS: trim battery/radio use in widgets and location** ✅ — two focused cuts
  to wasted power on the phone. (1) The configurable Lock Screen **self-care ring**
  rendered only its one check but fetched the whole Today glance — five parallel
  requests (departure, todos/now, self-care, outings, focus) — on every reload,
  *per pinned ring*, waking the radio for four responses it discarded. It now calls
  a self-care-only `Glance.fetchSelfCare()` (one request); the Home Screen glance
  widget and the watch relay, which use the rest, keep the full `fetch()`. (2)
  `LocationMonitor` now pins `desiredAccuracy` to `kCLLocationAccuracyHundredMeters`
  instead of leaving CoreLocation at best-accuracy GPS — all three feeds are coarse
  by design (significant-change ~500 m, `CLVisit` venue-level, geofences ~120 m), so
  the GPS chip no longer spins up to sharpen a fix nobody reads; comfortably within
  the geofence radius, so trigger reliability is unchanged.
