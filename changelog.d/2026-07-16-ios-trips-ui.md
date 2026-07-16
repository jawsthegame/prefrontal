- **iOS: Trips screen** ✅ — a native view of the closed-loop trip log, mirroring
  the web `/trips/board`. Reached from the **Me** tab, it reads `GET /trips` and
  shows the **open trip** (if you're out — elapsed + distance), the completed trips
  **awaiting a label** (each with when / duration / distance and a place-match
  hint), and **recent history** (label, duration, distance, life-domain, category,
  and the reflection outcome). Labeling opens a sheet — label + category + life
  area + an optional "how it went" note — posted in one `/webhooks/trip/retro`
  call (the note feeds the learning loop); a long-press on a history row re-files
  its life-domain (`/webhooks/trip/domain`). A footer summarizes the focus-balance
  rollup those trips feed and links into **Insights** for the full chart. New
  `Models/Trips.swift`, `Views/TripsView.swift`, `trips`/`retroTrip`/`setTripDomain`
  endpoints, and a Trips row on the Me tab. Client-only (build on a Mac); endpoints
  are the existing ingestion router.
