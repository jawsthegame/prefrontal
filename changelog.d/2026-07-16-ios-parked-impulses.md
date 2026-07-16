- **iOS: parked-impulse retro** ✅ — the native app now surfaces the Impulsivity
  module's captured-impulse review (`GET /impulses/parked`), previously reachable
  only over HTTP. A **Parked impulses** toolbar entry on the **Todos** tab (beside
  Stuck/avoided and Clarify) opens the batch of impulses you parked instead of
  chasing mid-task, led by the server's ready-to-speak retro line; triage each by
  **Keep** (a real one — leaves it in your todos) or **Drop** (noise — reuses the
  normal todo-drop, `POST /todos/{id}/drop`). Closes the capture-and-defer loop the
  app already opened via the capture Shortcut/Action Button. New
  `ParkedImpulsesView.swift`, `ParkedImpulse`/`ParkedImpulses` models, and
  `APIClient.parkedImpulses()`, covered by `APIClientTests`.
