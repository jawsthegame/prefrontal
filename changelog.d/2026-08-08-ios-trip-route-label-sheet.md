- **iOS: trip start/destination now shows in the Label sheet** ✅ — the
  "Where you went" route (`Home → … → destination`) rendered on the trip card but
  was blank in the **Label** sheet, so you couldn't see where a trip went while
  naming it. `TripRouteView` is a custom `FlowRow` layout; as a `Form` `Section`'s
  direct content a bare `Layout` gets an ambiguous width proposal and collapses to
  nothing (it worked on the card only because it already sat inside a `VStack`).
  Wrapped it in a definite-width container — matching the other in-`Form` FlowRows —
  so it lays out in the sheet too.
