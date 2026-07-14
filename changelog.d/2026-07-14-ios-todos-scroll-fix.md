- **iOS: fix Todos list not scrolling** ✅ — the per-row swipe-to-drop gesture was
  starving the enclosing `ScrollView`'s pan, so a full Todos list couldn't scroll
  at all. `SwipeToReveal`'s `DragGesture` used a 12pt `minimumDistance`, right on
  top of the ScrollView's own ~12–15pt pan threshold, so the two recognizers
  activated together and the simultaneous row gesture won. Raised the threshold to
  20pt so a vertical drag is claimed by the ScrollView first; horizontal
  swipe-to-reveal still works. Also fixes the same latent bug on the Calendar and
  Stuck & avoided lists, which share `SwipeToReveal`.
