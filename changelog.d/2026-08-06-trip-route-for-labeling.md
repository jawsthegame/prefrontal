- **See a trip's route when labeling it** ✅ — an unlabeled trip on `GET /trips`
  now carries a `route` (`{start, destination, stops}`, each point
  `{lat, lon, distance_m, at, place}`): the home start, the stops in order (each
  snapped to a curated place *name* when one is within the match radius), and the
  farthest-from-home stop as the real destination. So you can *see where you went*
  and label a loop even when no stop matched a saved place. The web trips board and
  the iOS trips list/label sheet render it as a `Home → … → destination` line — each
  point tappable to a map pin, the destination emphasized. New
  `prefrontal.trips.trip_route` (pure, offline, prefetch-friendly) powers it.
