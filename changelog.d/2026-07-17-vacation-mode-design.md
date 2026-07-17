- **Vacation mode — design proposal** 📝 — a design doc
  ([`docs/design/vacation-mode.md`](docs/design/vacation-mode.md)) resolving the
  open "automatic-by-location vs. manual toggle" question against the design
  commandments. Recommends **detect-and-confirm**: a location/calendar cue raises
  a one-tap confirmation to enter (conservative), returning home leans toward
  auto-resume (the safety-critical half — a forgotten manual *off* is what turns
  the tool into a dead mute), a visible banner throughout, and a manual
  `vacation on|off` kept as the escape hatch for staycations and false positives.
  Frames the mode itself as a **receptivity profile** that layers into
  `coaching.suppressed()` alongside quiet hours — reusing the existing
  `critical` / `_bypasses_silence_gates` keep-list — rather than a new kill
  switch. No code yet; proposal for a follow-up build.
