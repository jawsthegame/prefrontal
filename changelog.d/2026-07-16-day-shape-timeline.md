- **Visual day-shape** ✅ — the day rendered as a proportional **timeline** (the
  Structured / Tiimo pattern), so its *shape* is glanceable rather than a list you
  hold in your head. Today's commitments become fixed, anchored blocks; the open
  todos are fitted into the *forward* gaps between them (reusing the same
  `free_windows` + `suggest_for_windows` machinery the morning briefing already
  uses, so the two never disagree); the free time in between is drawn quietly. Each
  block's height tracks its length, a labelled line marks *now*, and the past is
  **dimmed, not scored** — the forgiving, no-guilt surface (roadmap M2's
  abandonment-trap guard). Built for how ADHD readers actually read a chart (the
  **CHI-2024** finding that colour/contrast don't decode reliably): meaning never
  rides on hue alone — every segment carries a redundant non-colour signal (a
  `kind` word + a glyph + a solid/dashed/dotted edge), and the CLI render is
  literally monochrome. New deterministic, model-free core `prefrontal.day_shape`
  (`build_day_shape` / `render_day_shape` / `day_shape_payload`), a read-only
  `GET /day` (safe to poll on a widget's timeline cadence), the `/day/board`
  timeline page (shared theme + nav), and `prefrontal day` on the CLI. Covered by
  `tests/test_day_shape.py`.
