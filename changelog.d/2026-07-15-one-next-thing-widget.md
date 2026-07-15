- **"One next thing" widget** ✅ — a home- / lock-screen glance that shows the
  *single* next action and nothing else. Where panic mode names every fire (for
  when you're already frozen), this is its quiet, always-on sibling: it reuses
  Prefrontal's honest prioritization rather than a flat list, walking a ladder —
  **leave now** (a travel-aware departure that says *go*, overriding even flow) →
  **mid-flight task pinned** (in a focus block or out on an errand, the glance
  reflects it back instead of yanking you toward something new) → the worst
  clock-bound fire (overdue todo / past-due blocker / urgent mail, scored exactly
  as `build_panic` scores them) → the **avoided-but-important todo** you keep
  skipping → something that simply fits your free window → all-clear. Whatever
  else is on the plate is withheld and collapsed into one reassuring "+N more can
  wait" (overwhelm is an indictment; one thing is an invitation). New
  deterministic, model-free core `prefrontal.next_thing` (`build_next_thing` /
  `render_next_thing`), a read-only `GET /next` (safe to poll every timeline
  refresh), and `prefrontal next` on the CLI. The `/todos/now` honest-pick body is
  factored into the shared `prefrontal.scheduling.suggest_now` so the glance and
  the endpoint never disagree. Native `OneNextThingWidget` (small / medium /
  Lock-Screen rectangular / inline / circular) with one-tap **Wrap up** and **I'm
  back** for a mid-flight focus / outing. Covered by `tests/test_next_thing.py`.
