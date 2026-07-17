- **Panic mode: "due today" todos no longer read as already overdue** ✅ — panic's
  deadline reader (`prefrontal.panic._parse_deadline`) parsed a stored deadline
  verbatim, so a full timestamp — which the chat assistant writes when it runs a
  date-only deadline through `to_utc` (`YYYY-MM-DD 00:00:00`) — was read as
  *midnight*, dropping a todo or blocker due *today* into the "🔴 Already behind"
  bucket (with a bogus overdue amount) all day, and letting it become the forced
  first step. It now mirrors the canonical `prefrontal.todos._parse_deadline`:
  the deadline is date-granular, so it slices to the date and anchors to
  end-of-local-day (23:59:59 local → UTC), matching every other surface. The two
  readers of the same field can no longer drift. Covered by a new case in
  `tests/test_panic.py`.
