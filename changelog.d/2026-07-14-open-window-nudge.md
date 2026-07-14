- **M3: calendar-gap "open window" nudge** ✅ — a new `open_window` coaching module
  that closes the *calendar-gaps* half of M3 divergence #6's remaining reach
  (`docs/roadmap-vision.md` §8). On the coaching tick, when you have a genuine free
  window *right now* before your next commitment and there's a worthwhile
  avoided-but-fitting todo, it emits one non-critical nudge offering to fill the gap
  ("You've got ~30 min before Team sync at 2:00 PM — good time to knock out the form
  you've been putting off?"). It is the proactive *push* twin of the `GET /todos/now`
  pull widget and reuses that recipe wholesale — `work_window_now` for the
  waking-hours bound, `available_now` for the minutes free until the next commitment,
  `filter_suggestible` + `fit_todos` (same context-conditioned `task_bias_resolver`),
  and `pick_now` over the `avoided_todos` ids — so there's no new scheduling math,
  just composition. Everything downstream (receptivity gate, dosage cap, quiet hours,
  channel choice, ack/episode logging) applies to the cue automatically like any
  other. The cue carries a **new `open_window` context key** (in `channel_targets`,
  so channel-response — and the receptivity gate through it — learn this cue's
  welcome-ness on its own terms) and is never critical. Two behaviors the module
  owns: **anti-spam** (a per-todo `dedup_key` + the engine's debounce, plus a small
  last-offered `todo_id:next_commitment_id` marker that suppresses an *identical*
  re-offer, so a long free afternoon doesn't re-fire every tick), and **Option A
  coordination with `task_paralysis`** (the module claims the offered todo id in the
  shared `CoachContext` from its `before_collect`, which runs before any `evaluate`,
  so `task_paralysis` stands down for that todo this tick regardless of collection
  order — the gap-anchored cue is strictly more informative). Adds the
  `coach_gap_min_minutes` tunable (default 15, seeded in `default_state` and read via
  `store.get_float`). Registered in `prefrontal/modules/__init__.py` so it respects
  mute + per-user disable like every other module. Covered by new tests in
  `tests/test_modules.py` (gap present, busy-now, gap-too-small, no-avoided-fit,
  off-hours, todo-window exclusion, the task_paralysis stand-down, identical-reoffer
  suppression, seeding) and `tests/test_coaching.py` (the cue flows through a full
  tick as a gated push, coordinates the stand-down, records the fire, and is
  receptivity-gated like any nudge).
