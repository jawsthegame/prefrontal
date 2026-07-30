- **iOS offline mode — read-side cache** ✅ — the read-side twin of the offline
  capture queue (#468). Off the tailnet, GETs used to throw and screens blanked
  out behind an error banner; now every successful GET body is cached in an
  App-Group-backed `ResponseCache` (`ios/Prefrontal/Config/ResponseCache.swift`,
  keyed by path + query, namespaced by a stable token hash, bounded 64 entries /
  2 MB, cleared on a server/user switch), and on a **transport** failure
  `APIClient.get` decodes and returns the last cached body instead of erroring —
  so Today, Todos, Calendar, Household, and the **widget** render last-known-good
  data offline. HTTP (4xx/5xx) and decoding errors are never masked by the cache.
  A companion `ConnectionStore` tracks fresh-vs-stale + the last sync time, mirrored
  on the main actor by `OfflineState`, and the primary read screens (Today,
  Todos, Calendar) show an "Offline — showing data from HH:MM" banner
  (`OfflineBanner`) while serving from cache. Covered by
  new `APIClientTests` cases (serve-on-failure, no-cache rethrow, HTTP-not-masked,
  token/query key namespacing) via a transport-failure `URLProtocol` stub.
