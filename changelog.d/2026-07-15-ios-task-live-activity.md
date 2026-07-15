- **iOS: persistent time-externalization Live Activity (M2)** ✅ — the Lock
  Screen / Dynamic Island timer now covers the **current task**, not just formal
  outings and focus sessions. When you start a todo and aren't in a focus block,
  its elapsed clock ("this has been running 22 min") ticks live on the Lock
  Screen and Dynamic Island with no app open and no push updates — the same
  self-ticking `Text(_, style: .timer)` the focus count-up already used, driven by
  the todo's `started_at`. **Exactly one** timer shows at a time, on a priority
  ladder — **outing → focus → task** — so the Lock Screen / Dynamic Island never
  carries two competing clocks (and the Dynamic Island's compact view can only
  render one anyway): an outing (physically out, hard back-by) outranks a focus
  block, which outranks a bare started task. Directly attacks time agnosia and the
  time-loss that hyperfocus magnifies. `SessionActivityAttributes` gains a `task`
  kind with kind-driven label/icon/count-direction helpers, and
  `LiveActivityManager` now re-identifies (switching tasks ends the stale activity
  and starts the new one); `sync` takes the current started todo (a new pure
  `Todo.current(in:)`, unit-tested in `LiveActivityTaskTests`), reconciled on every
  Today refresh.
