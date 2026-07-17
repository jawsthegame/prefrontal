- **Free-time surfaces no longer count someone-else's events as busy** ✅ — the
  "what can I do right now" suggestion (`prefrontal.scheduling.suggest_now`) and the
  visual day-shape (`prefrontal.day_shape.build_day_shape`) fed the *raw* commitment
  list into `free_windows`, so FYI events (a partner's appointment, a kid's recital)
  and placeholder/hold blocks ("Focus", "HOLD", …) were treated as busy — even
  though `prefrontal.commitments.is_attendable` says they don't consume *your* time
  and the cascade, departure, and availability engines all already exclude them. A
  genuinely-free user whose only calendar entry was a kid's recital was told "no
  free time right now" by `/todos/now` and next-thing, and the day-shape drew no
  free segment across that hour (neither committed nor available — it silently
  vanished). Both surfaces now filter to `is_attendable` before computing free
  windows, so FYI/hold blocks are still *drawn* but no longer carve holes in your
  open time. Covered by new cases in `tests/test_scheduling.py` and
  `tests/test_day_shape.py`.
